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
import html
import json
import mimetypes
import os
import re
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
MASQUES = DOSSIER / "masques.json"  # articles des sources retirés du site (bouton 🗑 de l'administrateur)
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
    return {"titre": titre, "resume": meta("og:description") or meta("description"),
            "image": meta("og:image"), "site": meta("og:site_name") or urllib.parse.urlparse(url).netloc,
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
    if chemin and not lien:
        lien = f"documents/{urllib.parse.quote(chemin.name)}"
    if not lien:  # texte seul : votre propre article
        lien = f"#publication-{uuid.uuid4().hex[:10]}"

    lignes = texte.split("\n")
    reste = "\n".join(lignes[1:]).strip()
    env = {
        "titre": (champs.get("titre") or "").strip() or infos.get("titre") or lignes[0][:150]
                 or (chemin.stem.replace("_", " ") if chemin else "Publication"),
        "resume": infos.get("resume") or (texte if champs.get("titre") else reste) or
                  (f"📄 Document : {chemin.name}" if chemin else ""),
        "image": infos.get("image", ""),
        "site": (champs.get("source") or "").strip() or infos.get("site") or "La Centrale",
        "lien": lien,
        "commentaire": (champs.get("commentaire") or "").strip() or (texte if infos.get("resume") else ""),
        "date": champs.get("date") or datetime.now(timezone.utc).isoformat(),
        "publie": False,
        "via": "studio",
    }

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
        "source": x.get("site") or "La Centrale", "theme": "Nos publications", "perso": True,
    } for x in envois]
