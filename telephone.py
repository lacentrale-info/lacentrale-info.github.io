"""Boîte de réception Telegram : les liens envoyés au bot depuis le téléphone.

Depuis le téléphone, on partage un article vers le bot Telegram de La Centrale.
À chaque lancement, centrale.py appelle relever_envois() : les nouveaux liens
sont ajoutés à la page (rubrique « Mes envois ») et, si publier_auto est vrai,
publiés tout de suite sur le canal.

Réglages dans config.json (jamais envoyé ailleurs) :
  telegram_jeton  : le jeton donné par @BotFather
  telegram_canal  : le canal où publier, ex. "@lacentrale_info"
  publier_auto    : true pour publier sur le canal sans validation
Le premier compte qui écrit au bot en devient le propriétaire : les messages
des autres comptes sont ignorés.

On peut aussi transférer au bot des messages Telegram (y compris des PDF) :
les fichiers sont rangés dans documents/ et republiés tels quels sur le canal.
"""
import hashlib
import html
import json
import mimetypes
import os
import re
import time
import uuid
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
CONFIG = DOSSIER / "config.json"  # réglages privés (jeton du bot) : reste sur l'ordinateur
ETAT = DOSSIER / "etat.json"  # suivi du bot sans secret, partagé avec la version en ligne
ENVOIS = DOSSIER / "envois.json"
BOITE = DOSSIER / "boite"  # publications déposées par le Studio en ligne
MASQUES = DOSSIER / "masques.json"
VIDEOS = (".mp4", ".webm", ".mov", ".m4v")  # articles des sources retirés du site (bouton 🗑 de l'administrateur)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Sites déjà condamnés en justice (ou dissous) : ajoutés à la page mais jamais publiés sur le canal.
SITES_NON_PUBLIES = ["civitas-institut.com", "unz.com"]
# Sites condamnés pour certains contenus seulement : publiés, avec un rappel de vérifier l'article.
SITES_A_VERIFIER = ["egaliteetreconciliation.fr", "kontrekulture.com"]

CONFIG_VIDE = {"telegram_jeton": "", "telegram_canal": "", "publier_auto": True}


def lire_config():
    """config.json sur l'ordinateur ; en ligne, le jeton vient des secrets GitHub (variables d'environnement)."""
    c = {**CONFIG_VIDE, **lire_json(ETAT, {}), **lire_json(CONFIG, {})}
    if os.environ.get("TELEGRAM_JETON"):
        c["telegram_jeton"] = os.environ["TELEGRAM_JETON"]
    if os.environ.get("TELEGRAM_CANAL"):
        c["telegram_canal"] = os.environ["TELEGRAM_CANAL"]
    return c


def sauver_config(c):
    ecrire_json(ETAT, {k: c[k] for k in ("dernier_update", "proprietaire_id") if k in c})
    if not os.environ.get("CI"):  # jamais de jeton écrit dans le dépôt en ligne
        ecrire_json(CONFIG, {k: v for k, v in c.items() if k not in ("dernier_update", "proprietaire_id")})


def lire_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def ecrire_json(chemin, valeur):
    chemin.write_text(json.dumps(valeur, ensure_ascii=False, indent=2), encoding="utf-8")


def api(jeton, methode, **params):
    donnees = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{jeton}/{methode}", donnees, timeout=30) as r:
        rep = json.loads(r.read())
    if not rep.get("ok"):
        raise RuntimeError(rep.get("description", "erreur Telegram"))
    return rep["result"]


def infos_page(url):
    """Titre, résumé et image d'un article, d'après ses balises de partage (og:)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        page = r.read(800000).decode("utf-8", "replace")

    def meta(nom):
        for motif in (rf'<meta[^>]+(?:property|name)=["\']{nom}["\'][^>]+content=["\']([^"\']*)',
                      rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{nom}["\']'):
            m = re.search(motif, page, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
        return ""

    titre = meta("og:title")
    if not titre:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        titre = html.unescape(m.group(1)).strip() if m else url
    # Facebook : « 2,8 M vues · 1,3 K réactions | Le vrai titre | Facebook » → « Le vrai titre »
    morceaux = [x.strip() for x in titre.split("|")]
    morceaux = [x for x in morceaux if x and x.lower() not in ("facebook", "instagram")
                and not re.search(r"\b(vues|views|réactions|reactions|j’aime|likes)\b", x, re.I)]
    titre = " | ".join(morceaux)
    if not titre and re.search(r"(facebook|instagram)\.com|fb\.watch", url):
        titre = ""  # Instagram ne donne rien sans connexion : le titre écrit dans le Studio sera utilisé
    elif not titre:
        titre = url
    return {"titre": titre, "resume": meta("og:description") or meta("description"),
            "image": meta("og:image"), "site": meta("og:site_name") or {"facebook.com": "Facebook", "fb.watch": "Facebook", "instagram.com": "Instagram"}.get(
                urllib.parse.urlparse(url).netloc.removeprefix("www.").removeprefix("m."), urllib.parse.urlparse(url).netloc),
            "publie_le": meta("article:published_time")}


def message_canal(env):
    e = html.escape
    texte = f"<b>{e(env['titre'])}</b>"
    if env.get("commentaire"):
        texte += f"\n\n💬 {e(env['commentaire'])}"
    if env.get("resume"):
        texte += f"\n\n{e(env['resume'][:600])}"
    return texte + f"\n\n📰 {e(env['site'])}\n🔗 {e(env['lien'])}"


def lien_origine(msg):
    """Lien t.me vers le message d'origine, s'il a été transféré depuis un canal public."""
    chat = msg.get("forward_from_chat") or (msg.get("forward_origin") or {}).get("chat") or {}
    num = msg.get("forward_from_message_id") or (msg.get("forward_origin") or {}).get("message_id")
    if chat.get("username") and num:
        return f"https://t.me/{chat['username']}/{num}"
    return ""


def enregistrer_document(jeton, msg, texte):
    """Télécharge le fichier reçu (20 Mo maximum, limite de Telegram) dans le dossier documents/."""
    doc = msg["document"]
    nom = re.sub(r'[\\/:*?"<>|]+', "_", doc.get("file_name") or f"document-{doc['file_unique_id']}")
    dossier = DOSSIER / "documents"
    dossier.mkdir(exist_ok=True)
    cible = dossier / nom
    if not cible.exists():
        try:
            chemin = api(jeton, "getFile", file_id=doc["file_id"])["file_path"]
            with urllib.request.urlopen(f"https://api.telegram.org/file/bot{jeton}/{chemin}", timeout=120) as r:
                cible.write_bytes(r.read())
        except Exception as e:
            api(jeton, "sendMessage", chat_id=msg["chat"]["id"], text=f"⚠️ Fichier non récupéré ({nom}) : {e}")
            return None
    titre = texte.split("\n")[0][:150] if texte else nom.rsplit(".", 1)[0].replace("_", " ")
    return {"titre": titre, "resume": f"📄 Document : {nom}" + (f" — {texte[:500]}" if texte else ""),
            "image": "", "site": "Document reçu", "lien": f"documents/{urllib.parse.quote(nom)}",
            "commentaire": "", "date": datetime.now(timezone.utc).isoformat(), "publie": False}


def publier_copie(jeton, canal, config, msg, env, expediteur):
    """Republie le message tel quel (fichier compris) sur le canal, puis confirme à l'expéditeur."""
    if config.get("publier_auto") and canal:
        try:
            api(jeton, "copyMessage", chat_id=canal, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
            env["publie"] = True
        except Exception as e:
            api(jeton, "sendMessage", chat_id=expediteur, text=f"⚠️ Pas publié sur le canal : {e}")
    api(jeton, "sendMessage", chat_id=expediteur,
        text=("✅ Publié sur le canal : " if env["publie"] else "📥 Ajouté à La Centrale : ") + env["titre"])


def relever_envois():
    """Récupère les liens envoyés au bot ; renvoie la liste de tous les envois (pour la page)."""
    config = lire_config()
    envois = lire_json(ENVOIS, [])
    jeton, canal = config.get("telegram_jeton"), config.get("telegram_canal")
    if not jeton:
        return envois, "bot Telegram non configuré (config.json)"

    try:
        maj = api(jeton, "getUpdates", offset=config.get("dernier_update", 0) + 1, timeout=0)
    except Exception as e:
        return envois, f"Telegram injoignable : {e}"

    nouveaux = 0
    for u in maj:
        config["dernier_update"] = u["update_id"]
        msg = u.get("message") or {}
        expediteur = (msg.get("from") or {}).get("id")
        if not expediteur:
            continue
        if not config.get("proprietaire_id"):
            config["proprietaire_id"] = expediteur
        if expediteur != config["proprietaire_id"]:
            continue
        texte = msg.get("text") or msg.get("caption") or ""
        # liens visibles + liens cachés derrière un mot (messages transférés depuis un canal)
        liens = re.findall(r"https?://\S+", texte)
        for ent in msg.get("entities", []) + msg.get("caption_entities", []):
            if ent.get("type") == "text_link" and ent["url"] not in liens:
                liens.append(ent["url"])

        if msg.get("document"):  # PDF ou autre fichier transféré
            env = enregistrer_document(jeton, msg, texte)
            if env and not any(x["lien"] == env["lien"] for x in envois):
                publier_copie(jeton, canal, config, msg, env, expediteur)
                envois.insert(0, env)
                nouveaux += 1
            continue
        if not liens:
            origine = lien_origine(msg)
            if origine and texte:  # message transféré d'un canal public, sans lien dedans
                env = {"titre": texte.split("\n")[0][:150], "resume": texte[:600], "image": "",
                       "site": (msg.get("forward_from_chat") or {}).get("title", "Telegram"), "lien": origine,
                       "commentaire": "", "date": datetime.now(timezone.utc).isoformat(), "publie": False}
                if not any(x["lien"] == origine for x in envois):
                    publier_copie(jeton, canal, config, msg, env, expediteur)
                    envois.insert(0, env)
                    nouveaux += 1
                continue
            api(jeton, "sendMessage", chat_id=expediteur,
                text="Envoyez-moi le lien d'un article (bouton Partager → Telegram → ce bot). "
                     "Vous pouvez ajouter un commentaire avant le lien.")
            continue
        commentaire = re.sub(r"https?://\S+", "", texte).strip()
        for lien in liens:
            if any(x["lien"] == lien for x in envois):
                continue
            try:
                env = infos_page(lien)
            except Exception:
                env = {"titre": lien, "resume": "", "image": "", "site": urllib.parse.urlparse(lien).netloc}
            env.update(lien=lien, commentaire=commentaire, date=datetime.now(timezone.utc).isoformat(), publie=False)
            interdit = any(s in urllib.parse.urlparse(lien).netloc for s in SITES_NON_PUBLIES)
            if interdit:
                api(jeton, "sendMessage", chat_id=expediteur,
                    text="⛔ Non publié sur le canal : ce site a déjà été condamné en justice. "
                         "Il est seulement ajouté à La Centrale pour lecture.")
            if any(s in urllib.parse.urlparse(lien).netloc for s in SITES_A_VERIFIER):
                api(jeton, "sendMessage", chat_id=expediteur,
                    text="⚠️ Rappel : ce site a déjà été condamné pour certains contenus. "
                         "Vérifiez que cet article ne pose pas de problème.")
            if config.get("publier_auto") and canal and not interdit:
                try:
                    api(jeton, "sendMessage", chat_id=canal, text=message_canal(env), parse_mode="HTML")
                    env["publie"] = True
                except Exception as e:
                    api(jeton, "sendMessage", chat_id=expediteur, text=f"⚠️ Pas publié sur le canal : {e}")
            envois.insert(0, env)
            nouveaux += 1
            api(jeton, "sendMessage", chat_id=expediteur,
                text=("✅ Publié sur le canal : " if env["publie"] else "📥 Ajouté à La Centrale : ") + env["titre"])

    sauver_config(config)
    ecrire_json(ENVOIS, envois)
    return envois, f"{nouveaux} nouveau(x) envoi(s) reçu(s)"


def couper(texte, n=140):
    """Coupe un texte trop long à la fin d'un mot, avec « … » (jamais au milieu d'un mot)."""
    texte = " ".join((texte or "").split())
    if len(texte) <= n:
        return texte
    return texte[:n].rsplit(" ", 1)[0].rstrip(" ,;:.-–—") + "…"


def premiere_phrase(texte, n=140):
    """Première ligne d'un texte, réduite à sa première phrase : sert de titre à un post Telegram."""
    ligne = next((l.strip() for l in (texte or "").split("\n") if l.strip()), "")
    m = re.match(r"(.{20,}?[.!?…])(\s|$)", ligne)
    return couper(m.group(1) if m else ligne, n)


def garder_image(url):
    """Copie l'image d'une publication dans documents/ : les liens d'images de Telegram expirent
    au bout de quelques jours et certains sites refusent que leurs images s'affichent ailleurs."""
    if not url or not url.startswith("http"):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            sorte, donnees = r.headers.get_content_type(), r.read(8_000_000)
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(sorte)
        if not ext or len(donnees) < 1000:
            return url
        nom = "pub-" + hashlib.sha1(url.encode()).hexdigest()[:12] + ext
        (DOSSIER / "documents").mkdir(exist_ok=True)
        (DOSSIER / "documents" / nom).write_bytes(donnees)
        return f"documents/{nom}"
    except Exception:
        return url


def nom_site(site, lien):
    """« pierreantoineplaquevent.substack.com » → « Pierre-Antoine Plaquevent » (nom de la source connue)."""
    site = (site or "").strip()
    if not site or " " in site or "." not in site:
        return site or "La Centrale"
    domaine = site.lower().removeprefix("www.")
    for s in lire_json(DOSSIER / "sources.json", []):
        hote = urllib.parse.urlparse(s.get("flux", "")).netloc.lower().removeprefix("www.")
        if hote == domaine and "news.google" not in hote and "youtube" not in hote:
            return s["nom"]
    return domaine


def creer_publication(champs, chemin=None):
    """Publication du Studio (sur l'ordinateur ou en ligne) : mise en page, Telegram, envois.json.

    champs : texte, lien, titre, source, commentaire, telegram ("1" pour publier sur le canal)
    chemin : document déjà enregistré dans documents/ (PDF, image…), facultatif
    """
    lien = (champs.get("lien") or "").strip()
    texte = (champs.get("texte") or "").strip()
    if not lien:
        m = re.search(r"https?://\S+", texte)
        if m:
            lien = m.group(0)
            texte = (texte[:m.start()] + texte[m.end():]).strip()
    if not (lien or texte or chemin):
        raise ValueError("Collez un lien, un texte ou ajoutez un fichier.")

    infos = {}
    if lien:
        try:
            infos = infos_page(lien)
        except Exception:
            infos = {"site": urllib.parse.urlparse(lien).netloc}
    # post Telegram : la page ne donne que le nom de la chaîne comme titre → titre = première phrase du post
    if re.match(r"https?://(t|telegram)\.me/", lien) and infos.get("resume"):
        infos["site"] = infos.get("titre") or "Telegram"
        infos["titre"] = premiere_phrase(infos["resume"])
    if chemin and not lien:
        lien = f"documents/{urllib.parse.quote(chemin.name)}"
    if not lien:  # texte seul : votre propre article
        lien = f"#publication-{uuid.uuid4().hex[:10]}"

    lignes = texte.split("\n")
    reste = "\n".join(lignes[1:]).strip()
    env = {
        "titre": (champs.get("titre") or "").strip() or couper(infos.get("titre"), 150) or couper(lignes[0], 150)
                 or (chemin.stem.replace("_", " ") if chemin else "Publication"),
        "resume": infos.get("resume") or (texte if champs.get("titre") else reste) or
                  (("🎬 Vidéo" if chemin.suffix.lower() in VIDEOS else f"📄 Document : {chemin.name}")
                   if chemin and not champs.get("rubrique") else ""),
        "image": garder_image(infos.get("image")) or (champs.get("image") or "").strip(),  # sinon vignette d'une vidéo du Studio
        "site": (champs.get("source") or "").strip() or nom_site(infos.get("site"), lien),
        "lien": lien,
        "commentaire": (champs.get("commentaire") or "").strip() or (texte if infos.get("resume") else ""),
        "date": champs.get("date") or datetime.now(timezone.utc).isoformat(),
        "publie": False,
        "via": "studio",
    }
    if champs.get("rubrique"):  # ex. « Humour » : la publication va dans cet onglet
        env["theme"] = champs["rubrique"]
    if chemin and not env["image"] and chemin.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        env["image"] = f"documents/{urllib.parse.quote(chemin.name)}"  # une image publiée sert aussi de vignette

    message = "Ajouté à La Centrale."
    c = lire_config()
    if str(champs.get("telegram")) in ("1", "True", "true") and c.get("telegram_jeton") and c.get("telegram_canal"):
        if any(s in urllib.parse.urlparse(lien).netloc for s in SITES_NON_PUBLIES):
            message += " Pas publié sur Telegram : site déjà condamné en justice."
        else:
            try:
                if chemin:
                    envoyer_document(c["telegram_jeton"], c["telegram_canal"], chemin, message_canal(env))
                else:
                    api(c["telegram_jeton"], "sendMessage", chat_id=c["telegram_canal"],
                        text=message_canal(env), parse_mode="HTML")
                env["publie"] = True
                message += " Publié sur le canal Telegram."
            except Exception as e:
                message += f" Telegram : échec ({e})."

    envois = [x for x in lire_json(ENVOIS, []) if x["lien"] != env["lien"]]
    envois.insert(0, env)
    ecrire_json(ENVOIS, envois)
    return env, message


def retirer_publication(lien):
    ecrire_json(ENVOIS, [x for x in lire_json(ENVOIS, []) if x["lien"] != lien])


def envoyer_document(jeton, canal, chemin, legende):
    """sendDocument : envoi du fichier lui-même sur le canal."""
    limite = uuid.uuid4().hex
    corps = b""
    for nom, valeur in (("chat_id", canal), ("caption", legende[:1024]), ("parse_mode", "HTML")):
        corps += f'--{limite}\r\nContent-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'.encode()
    corps += (f'--{limite}\r\nContent-Disposition: form-data; name="document"; filename="{chemin.name}"\r\n'
              f'Content-Type: {mimetypes.guess_type(chemin.name)[0] or "application/octet-stream"}\r\n\r\n').encode()
    corps += chemin.read_bytes() + f"\r\n--{limite}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{jeton}/sendDocument", corps,
                                 {"Content-Type": f"multipart/form-data; boundary={limite}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        rep = json.loads(r.read())
    if not rep.get("ok"):
        raise RuntimeError(rep.get("description", "erreur Telegram"))


# ---------- Sources ajoutées depuis le Studio (à distance) ----------
SOURCES = DOSSIER / "sources.json"
RAPPORT = DOSSIER / "sources-rapport.json"  # résultat des derniers ajouts/retraits, affiché dans le Studio


def ecrire_sources(sources):
    """Même mise en forme que le fichier écrit à la main : une source par ligne."""
    SOURCES.write_text("[\n" + ",\n".join("  " + json.dumps(s, ensure_ascii=False) for s in sources) + "\n]\n",
                       encoding="utf-8")


def noter(adresse, ok, message, nom=""):
    rapport = lire_json(RAPPORT, [])
    rapport.insert(0, {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "adresse": adresse, "nom": nom, "ok": ok, "message": message})
    ecrire_json(RAPPORT, rapport[:30])


def nom_de_page(page, defaut):
    for motif in (r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                  r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name',
                  r"<title[^>]*>([^<]+)</title>"):
        m = re.search(motif, page, re.I)
        if m:
            nom = re.split(r"\s+[|–—-]\s+", html.unescape(m.group(1)).strip())[0].strip()
            if 2 <= len(nom) <= 60:
                return nom
    return defaut


def chercher_source(adresse, theme, nom=""):
    """Trouve tout seul comment lire une source à partir d'une simple adresse (site, blog, Substack,
    chaîne YouTube, canal Telegram public) ou d'un nom de personne (ses dernières vidéos YouTube).
    Renvoie (source, nombre d'articles lus) ou lève ValueError avec une explication."""
    from centrale import analyser_flux, recuperer, relais_google, telecharger
    adresse = adresse.strip().rstrip("/")
    lire_page = lambda u: telecharger(u).decode("utf-8", "replace")

    def essayer(source):
        s, articles, erreur = recuperer(source)
        return len(articles) if not erreur and articles else 0

    if not re.match(r"https?://", adresse):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", adresse, re.I):
            adresse = "https://" + adresse
        else:  # un nom de personne : ses vidéos récentes sur YouTube
            source = {"nom": nom or adresse, "recherche_youtube": adresse, "theme": theme}
            n = essayer(source)
            if not n:
                raise ValueError("aucune vidéo trouvée sur YouTube pour ce nom")
            return source, n

    url = urllib.parse.urlparse(adresse)
    hote = url.netloc.lower().removeprefix("www.").removeprefix("m.")

    if hote in ("t.me", "telegram.me"):  # canal Telegram public
        morceaux = [m for m in url.path.split("/") if m and m != "s"]
        if not morceaux:
            raise ValueError("adresse de canal Telegram incomplète")
        source = {"nom": nom or morceaux[0], "telegram": morceaux[0], "theme": theme}
        try:
            titre = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', lire_page(f"https://t.me/s/{morceaux[0]}"))
            source["nom"] = nom or (html.unescape(titre.group(1)).strip() if titre else morceaux[0])
        except Exception:
            pass
        n = essayer(source)
        if not n:
            raise ValueError("canal Telegram introuvable ou privé (seuls les canaux publics sont lisibles)")
        return source, n

    if hote.endswith("youtube.com") or hote == "youtu.be":
        m = re.search(r"/channel/(UC[\w-]{22})", adresse)
        page = ""
        if not m:
            page = lire_page(adresse)
            m = (re.search(r'"externalId":"(UC[\w-]{22})"', page) or re.search(r'"channelId":"(UC[\w-]{22})"', page)
                 or re.search(r"youtube\.com/channel/(UC[\w-]{22})", page))
        if not m:
            raise ValueError("chaîne YouTube introuvable : collez l'adresse de la chaîne (youtube.com/@…)")
        if not page:
            try:
                page = lire_page(f"https://www.youtube.com/channel/{m.group(1)}")
            except Exception:
                page = ""
        titre = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
        source = {"nom": nom or (html.unescape(titre.group(1)) if titre else hote),
                  "flux": f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}", "theme": theme}
        n = essayer(source)
        if not n:
            raise ValueError("la chaîne YouTube ne donne aucune vidéo")
        return source, n

    # site ou blog : on cherche son flux RSS (annoncé dans la page, puis aux adresses habituelles)
    base = f"{url.scheme}://{url.netloc}"
    candidats, page = [], ""
    if hote.endswith(".substack.com"):
        candidats.append(f"{base}/feed")
    try:
        page = lire_page(adresse)
        for balise in re.findall(r"<link[^>]+application/(?:rss|atom)\+xml[^>]*>", page, re.I):
            href = re.search(r'href=["\']([^"\']+)', balise)
            if href and "comment" not in href.group(1).lower():
                candidats.append(urllib.parse.urljoin(adresse + "/", html.unescape(href.group(1))))
    except Exception:
        pass  # page bloquée : on essaie quand même les adresses habituelles
    if hote.endswith(".blogspot.com"):
        candidats.append(f"{base}/feeds/posts/default")
    candidats += [f"{base}/{c}" for c in ("feed/", "feed", "rss", "rss.xml", "feed.xml", "atom.xml", "index.xml")]
    nom = nom or nom_de_page(page, hote)
    candidats = list(dict.fromkeys(candidats))
    for essai, liste in enumerate((candidats, candidats[:4])):  # 2e passage : le site a pu refuser un instant
        if essai:
            time.sleep(4)
        for flux in liste:
            try:
                if analyser_flux(telecharger(flux)):
                    source = {"nom": nom, "flux": flux, "theme": theme}
                    n = essayer(source)
                    if n:
                        return source, n
            except Exception:
                continue
    # aucun flux lisible d'ici : relais par Google Actualités
    try:
        if len(relais_google(hote)) >= 2:
            source = {"nom": nom, "flux": f"https://news.google.com/rss/search?q=site:{hote}&hl=en-GB&gl=GB&ceid=GB:en",
                      "theme": theme}
            return source, essayer(source)
    except Exception:
        pass
    raise ValueError("aucun flux d'articles trouvé (le site bloque peut-être les robots)")


def ajouter_source(demande):
    adresse, theme = (demande.get("adresse") or "").strip(), (demande.get("theme") or "Divers").strip()
    if not adresse:
        return
    sources = lire_json(SOURCES, [])
    try:
        source, n = chercher_source(adresse, theme, (demande.get("nom") or "").strip())
    except Exception as e:
        noter(adresse, False, f"Pas ajoutée : {e}.")
        return
    cle = lambda s: s.get("flux") or s.get("page") or s.get("telegram") or s.get("recherche_youtube")
    deja = next((s for s in sources if cle(s) == cle(source) or s["nom"].lower() == source["nom"].lower()), None)
    if deja:
        noter(adresse, False, f"Déjà présente sous le nom « {deja['nom']} ».", deja["nom"])
        return
    sources.append(source)
    ecrire_sources(sources)
    noter(adresse, True, f"Ajoutée : {n} article{'s' if n > 1 else ''} trouvé{'s' if n > 1 else ''}.", source["nom"])


def retirer_source(nom):
    sources = lire_json(SOURCES, [])
    restantes = [s for s in sources if s["nom"] != nom]
    if len(restantes) < len(sources):
        ecrire_sources(restantes)
        noter(nom, True, "Retirée du site.", nom)


def traiter_boite():
    """Publications déposées par le Studio en ligne dans boite/ (un fichier .json par action)."""
    if not BOITE.exists():
        return "boîte vide"
    n = 0
    for f in sorted(BOITE.glob("*.json")):
        demande = lire_json(f, {})
        try:
            if demande.get("type") == "suppression":
                retirer_publication(demande["lien"])
            elif demande.get("type") == "masquer":  # article d'une source retiré du site depuis la page
                masques = [m for m in lire_json(MASQUES, []) if m["lien"] != demande["lien"]]
                masques.insert(0, {"lien": demande["lien"], "titre": demande.get("titre", ""),
                                   "source": demande.get("source", ""),
                                   "date": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                ecrire_json(MASQUES, masques)
            elif demande.get("type") == "source":  # nouvelle source envoyée depuis le Studio
                ajouter_source(demande)
            elif demande.get("type") == "retirer-source":
                retirer_source(demande.get("nom", ""))
            elif demande.get("type") == "remettre":
                ecrire_json(MASQUES, [m for m in lire_json(MASQUES, []) if m["lien"] != demande["lien"]])
            else:
                doc = demande.get("document")
                chemin = DOSSIER / doc if doc else None
                creer_publication(demande, chemin if chemin and chemin.exists() else None)
            n += 1
        except Exception as e:
            print(f"  Boîte : {f.name} ignoré ({e})")
        f.unlink()
    return f"{n} action(s) du Studio en ligne"


def lire_envois():
    return lire_json(ENVOIS, [])


def en_articles(envois):
    """Convertit vos publications au format des articles de centrale.py."""
    return [{
        "titre": x["titre"], "lien": x["lien"],
        "resume": (f"💬 {x['commentaire']} — " if x.get("commentaire") else "")
                  + ((x.get("resume") or "") if x["lien"].startswith("#") else (x.get("resume") or "")[:260]),
        "image": x.get("image", ""), "date": datetime.fromisoformat(x["date"]),
        "source": nom_site(x.get("site"), x["lien"]), "theme": x.get("theme") or "Nos publications", "perso": True,
    } for x in envois]
