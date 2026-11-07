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
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
CONFIG = DOSSIER / "config.json"
ENVOIS = DOSSIER / "envois.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Sites déjà condamnés en justice (ou dissous) : ajoutés à la page mais jamais publiés sur le canal.
SITES_NON_PUBLIES = ["egaliteetreconciliation.fr", "kontrekulture.com", "civitas-institut.com", "unz.com"]

CONFIG_VIDE = {"telegram_jeton": "", "telegram_canal": "", "publier_auto": True}


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
    config = lire_json(CONFIG, None)
    if config is None:
        ecrire_json(CONFIG, CONFIG_VIDE)
        config = dict(CONFIG_VIDE)
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

    ecrire_json(CONFIG, config)
    ecrire_json(ENVOIS, envois)
    return envois, f"{nouveaux} nouveau(x) envoi(s) reçu(s)"


def en_articles(envois):
    """Convertit les envois au format des articles de centrale.py."""
    return [{
        "titre": x["titre"], "lien": x["lien"],
        "resume": (f"💬 {x['commentaire']} — " if x.get("commentaire") else "") + (x.get("resume") or "")[:260],
        "image": x.get("image", ""), "date": datetime.fromisoformat(x["date"]),
        "source": "Mes envois", "theme": "Mes envois",
    } for x in envois]
