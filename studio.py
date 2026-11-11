"""Le Studio de La Centrale : l'appli pour publier depuis le téléphone ou la tablette.

On y colle un lien, un texte ou un PDF ; le Studio prépare la mise en page
(titre, image, résumé, source), l'ajoute à « Nos publications » sur La Centrale
et, si le bot est configuré, le publie sur le canal Telegram.

Lancement : python studio.py   (ou double-clic sur studio.bat)
Puis, sur le téléphone connecté au même Wi-Fi, ouvrir l'adresse affichée
et « Ajouter à l'écran d'accueil » pour l'avoir comme une appli.
Accès protégé par le code « studio_code » de config.json.
"""
import hashlib
import json
import mimetypes
import re
import secrets
import socket
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from centrale import reconstruire
from telephone import ENVOIS, creer_publication, infos_page, lire_config, lire_json, retirer_publication, sauver_config

DOSSIER = Path(__file__).resolve().parent
DOCUMENTS = DOSSIER / "documents"
PORT = 8800
TAILLE_MAX = 50 * 1024 * 1024  # 50 Mo par envoi
verrou = threading.Lock()


def config():
    c = lire_config()
    if not c.get("studio_code"):
        c["studio_code"] = f"{secrets.randbelow(10**6):06d}"
        sauver_config(c)
    return c


def jeton_session(code):
    return hashlib.sha256(f"la-centrale:{code}".encode()).hexdigest()


def nom_de_fichier(nom):
    nom = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", Path(nom or "document").name).strip(" .") or "document"
    cible = DOCUMENTS / nom
    if cible.exists():  # ne jamais écraser un document déjà publié
        cible = DOCUMENTS / f"{cible.stem}-{uuid.uuid4().hex[:6]}{cible.suffix}"
    return cible


def publier(champs, fichier):
    """Enregistre le fichier éventuel, crée la publication puis refait la page."""
    chemin = None
    if fichier:
        DOCUMENTS.mkdir(exist_ok=True)
        chemin = nom_de_fichier(fichier[0])
        chemin.write_bytes(fichier[1])
    with verrou:
        env, message = creer_publication(champs, chemin)
        reconstruire()
    return env, message


def supprimer(lien):
    with verrou:
        retirer_publication(lien)
        reconstruire()


class Studio(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def connecte(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return "studio" in cookie and cookie["studio"].value == jeton_session(config()["studio_code"])

    def repondre(self, code, contenu, type_="text/html; charset=utf-8", entetes=None):
        donnees = contenu if isinstance(contenu, bytes) else contenu.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(donnees)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(donnees)

    def json(self, valeur, code=200):
        self.repondre(code, json.dumps(valeur, ensure_ascii=False), "application/json; charset=utf-8")

    def fichier(self, chemin):
        if not chemin.is_file():
            return self.repondre(404, "Introuvable")
        self.repondre(200, chemin.read_bytes(), mimetypes.guess_type(chemin.name)[0] or "application/octet-stream")

    def corps(self):
        taille = int(self.headers.get("Content-Length", 0))
        if taille > TAILLE_MAX:
            raise ValueError("Fichier trop gros (50 Mo maximum).")
        return self.rfile.read(taille)

    def do_GET(self):
        chemin = urllib.parse.urlparse(self.path).path
        if chemin == "/manifest.json":
            return self.repondre(200, MANIFESTE, "application/manifest+json")
        if chemin == "/icone.svg":
            return self.repondre(200, ICONE, "image/svg+xml")
        if not self.connecte():
            return self.repondre(200, PAGE_CODE)
        if chemin == "/":
            return self.repondre(200, PAGE_STUDIO)
        if chemin == "/site":
            return self.fichier(DOSSIER / "index.html")
        if chemin.startswith("/documents/"):
            return self.fichier(DOCUMENTS / Path(urllib.parse.unquote(chemin[len("/documents/"):])).name)
        if chemin == "/publications":
            c = config()
            return self.json({"publications": lire_json(ENVOIS, [])[:50],
                              "telegram": bool(c.get("telegram_jeton") and c.get("telegram_canal"))})
        self.repondre(404, "Introuvable")

    def do_POST(self):
        chemin = urllib.parse.urlparse(self.path).path
        try:
            if chemin == "/connexion":
                code = urllib.parse.parse_qs(self.corps().decode()).get("code", [""])[0].strip()
                if secrets.compare_digest(code, config()["studio_code"]):
                    return self.repondre(303, "", entetes={
                        "Location": "/", "Set-Cookie": f"studio={jeton_session(code)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"})
                return self.repondre(200, PAGE_CODE.replace("<!--erreur-->", '<p class="erreur">Code incorrect</p>'))
            if not self.connecte():
                return self.json({"erreur": "Session expirée, rechargez la page."}, 401)
            if chemin == "/apercu":
                lien = json.loads(self.corps()).get("lien", "")
                return self.json(infos_page(lien))
            if chemin == "/supprimer":
                supprimer(json.loads(self.corps())["lien"])
                return self.json({"ok": True})
            if chemin == "/publier":
                corps = self.corps()
                msg = BytesParser(policy=HTTP).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + corps)
                champs, fichier = {}, None
                for partie in msg.iter_parts():
                    nom = partie.get_param("name", header="content-disposition")
                    if partie.get_filename():
                        contenu = partie.get_payload(decode=True)
                        if contenu:
                            fichier = (partie.get_filename(), contenu)
                    else:
                        champs[nom] = partie.get_payload(decode=True).decode("utf-8", "replace")
                env, message = publier(champs, fichier)
                return self.json({"ok": True, "message": message, "publication": env})
            self.repondre(404, "Introuvable")
        except Exception as e:
            self.json({"erreur": str(e)}, 400)


def adresse_locale():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


ICONE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#16150f"/>
<text x="256" y="300" font-family="Georgia,serif" font-size="210" font-weight="800" fill="#ff6b5e" text-anchor="middle">LC</text>
<rect x="96" y="360" width="320" height="14" fill="#b3261e"/></svg>"""

MANIFESTE = json.dumps({
    "name": "Le Studio – La Centrale", "short_name": "Studio", "start_url": "/", "display": "standalone",
    "background_color": "#16150f", "theme_color": "#16150f",
    "icons": [{"src": "/icone.svg", "sizes": "any", "type": "image/svg+xml"}],
}, ensure_ascii=False)

ENTETE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#16150f"><link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icone.svg"><link rel="apple-touch-icon" href="/icone.svg">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Le Studio – La Centrale</title>
<style>
:root{--fond:#f6f3ee;--carte:#fff;--texte:#1d1b18;--doux:#6b645a;--ligne:#e2dcd2;--accent:#b3261e;--ok:#1f7a4d;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--fond:#16150f;--carte:#211f19;--texte:#ece7dd;--doux:#a39b8d;--ligne:#36322a;--accent:#ff6b5e;--ok:#3fb07a;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--fond);color:var(--texte);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:0 16px calc(24px + env(safe-area-inset-bottom))}
header{background:#16150f;color:#ece7dd;margin:0 -16px 18px;padding:calc(14px + env(safe-area-inset-top)) 16px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px}
header h1{margin:0;font:800 22px Georgia,serif;letter-spacing:.04em;text-transform:uppercase}
header h1 span{color:#ff6b5e}
header a{color:#ece7dd;font-size:14px;border:1px solid #555;border-radius:20px;padding:5px 12px;text-decoration:none}
main{max-width:640px;margin:0 auto}
label{display:block;font-size:13px;font-weight:600;color:var(--doux);margin:14px 0 5px}
textarea,input[type=text],input[type=password]{width:100%;padding:12px;border:1px solid var(--ligne);border-radius:10px;background:var(--carte);color:var(--texte);font:inherit}
textarea{min-height:120px;resize:vertical}
button{font:inherit;cursor:pointer;border:0;border-radius:10px;padding:13px 18px;background:var(--accent);color:#fff;font-weight:700}
button.second{background:var(--carte);color:var(--texte);border:1px solid var(--ligne);font-weight:500}
.ligne{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.fichier{display:flex;align-items:center;gap:10px;border:2px dashed var(--ligne);border-radius:10px;padding:14px;background:var(--carte);cursor:pointer}
.fichier input{display:none}
.apercu{margin-top:16px;background:var(--carte);border:1px solid var(--ligne);border-radius:12px;overflow:hidden}
.apercu img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block}
.apercu .c{padding:12px 14px}
.apercu .src{font-size:12px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.04em}
.apercu h3{font:700 19px/1.3 Georgia,serif;margin:4px 0 6px}
.apercu p{margin:0;color:var(--doux);font-size:14px}
.apercu .com{color:var(--texte);font-style:italic;margin-bottom:6px}
details{margin-top:6px}summary{cursor:pointer;color:var(--doux);font-size:14px;padding:8px 0}
.case{display:flex;gap:10px;align-items:center;margin:16px 0 4px;font-size:15px}
.case input{width:20px;height:20px}
#publier{width:100%;margin-top:14px;font-size:17px;padding:15px}
#message{margin-top:12px;padding:12px;border-radius:10px;background:var(--carte);border-left:4px solid var(--ok)}
#message.erreur{border-left-color:var(--accent)}
h2{font:700 13px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:8px;margin:34px 0 10px}
.pub{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--ligne)}
.pub div{flex:1;min-width:0}
.pub b{display:block;font-family:Georgia,serif;overflow-wrap:anywhere}
.pub small{color:var(--doux)}
.pub button{padding:6px 10px;font-size:13px}
.erreur{color:var(--accent)}
[hidden]{display:none!important}
</style></head><body>"""

PAGE_CODE = ENTETE + """<header><h1>Le <span>Studio</span></h1></header><main>
<form method="post" action="/connexion" style="margin-top:40px">
<label for="code">Code d'accès du Studio</label>
<input id="code" name="code" type="password" inputmode="numeric" autocomplete="current-password" autofocus>
<!--erreur--><button style="width:100%;margin-top:14px">Entrer</button></form></main></body></html>"""

PAGE_STUDIO = ENTETE + """<header><h1>Le <span>Studio</span></h1><a href="/site" target="_blank">Voir La Centrale ↗</a></header>
<main>
<form id="form">
  <label for="texte">Collez un lien, un article ou votre texte</label>
  <textarea id="texte" name="texte" placeholder="https://… ou votre texte"></textarea>
  <label class="fichier" for="fichier">📎 <span id="nom-fichier">Ajouter un PDF, une image…</span><input id="fichier" name="fichier" type="file"></label>
  <label for="commentaire">Votre commentaire (facultatif)</label>
  <input id="commentaire" name="commentaire" type="text" placeholder="Ce que vous voulez dire en publiant">
  <details><summary>Modifier le titre ou la source</summary>
    <label for="titre">Titre</label><input id="titre" name="titre" type="text" placeholder="Trouvé automatiquement">
    <label for="source">Source</label><input id="source" name="source" type="text" placeholder="Trouvée automatiquement">
  </details>
  <div class="apercu" id="apercu" hidden></div>
  <label class="case" id="case-tg" hidden><input type="checkbox" id="telegram" checked> Publier aussi sur le canal Telegram</label>
  <button id="publier" type="submit">Publier</button>
  <div id="message" hidden></div>
</form>
<h2>Mes publications</h2>
<div id="liste"><p style="color:var(--doux)">Chargement…</p></div>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
let infos={},minuteur=null,dernierLien='';
function lienDans(t){const m=(t||'').match(/https?:\\/\\/\\S+/);return m?m[0]:''}
function majApercu(){
  const t=$('texte').value.trim(),lien=lienDans(t),f=$('fichier').files[0];
  const titre=$('titre').value||infos.titre||(lien?'':t.split('\\n')[0])||(f?f.name:'');
  const source=$('source').value||infos.site||(f&&!lien?'Document':'La Centrale');
  const resume=lien?(infos.resume||''):t.split('\\n').slice(1).join(' ');
  const com=$('commentaire').value;
  $('apercu').hidden=!(titre||lien||f);
  $('apercu').innerHTML=(infos.image?`<img src="${esc(infos.image)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">`:'')+
    `<div class="c"><div class="src">${esc(source)}${f?' · 📄 '+esc(f.name):''}</div><h3>${esc(titre)}</h3>`+
    (com?`<p class="com">💬 ${esc(com)}</p>`:'')+`<p>${esc(resume.slice(0,280))}</p></div>`;
}
async function chercher(){
  const lien=lienDans($('texte').value);
  if(!lien||lien===dernierLien){majApercu();return}
  dernierLien=lien;infos={titre:'Recherche de l\\'article…'};majApercu();
  try{const r=await fetch('/apercu',{method:'POST',body:JSON.stringify({lien})});infos=await r.json();if(infos.erreur)infos={}}
  catch(e){infos={}}
  majApercu();
}
$('texte').oninput=()=>{clearTimeout(minuteur);minuteur=setTimeout(chercher,500)};
['commentaire','titre','source'].forEach(id=>$(id).oninput=majApercu);
$('fichier').onchange=()=>{const f=$('fichier').files[0];$('nom-fichier').textContent=f?f.name:'Ajouter un PDF, une image…';majApercu()};
$('form').onsubmit=async e=>{
  e.preventDefault();
  const d=new FormData($('form'));d.set('telegram',$('telegram').checked&&!$('case-tg').hidden?'1':'0');
  $('publier').disabled=true;$('publier').textContent='Publication…';
  const m=$('message');
  try{
    const r=await fetch('/publier',{method:'POST',body:d});const j=await r.json();
    if(!r.ok||j.erreur)throw new Error(j.erreur||'erreur');
    m.className='';m.textContent='✅ '+j.message;$('form').reset();infos={};dernierLien='';
    $('nom-fichier').textContent='Ajouter un PDF, une image…';$('apercu').hidden=true;charger();
  }catch(err){m.className='erreur';m.textContent='⚠️ '+err.message}
  m.hidden=false;$('publier').disabled=false;$('publier').textContent='Publier';
};
async function charger(){
  const r=await fetch('/publications');const j=await r.json();
  $('case-tg').hidden=!j.telegram;
  $('liste').innerHTML=j.publications.length?j.publications.map(p=>{
    const lien=p.lien.startsWith('documents/')?'/'+p.lien:p.lien.startsWith('#')?'/site'+p.lien:p.lien;
    return `<div class="pub"><div><b>${esc(p.titre)}</b><small>${esc(p.site||'')} · ${new Date(p.date).toLocaleString('fr-FR',{dateStyle:'short',timeStyle:'short'})}${p.publie?' · ✈ Telegram':''}</small></div>
    <a href="${esc(lien)}" target="_blank"><button class="second" type="button">Voir</button></a>
    <button class="second" type="button" data-lien="${esc(p.lien)}">Retirer</button></div>`}).join(''):'<p style="color:var(--doux)">Rien encore. Votre première publication apparaîtra ici.</p>';
  document.querySelectorAll('[data-lien]').forEach(b=>b.onclick=async()=>{
    if(b.dataset.ok!=='1'){b.dataset.ok='1';b.textContent='Confirmer ?';return}
    await fetch('/supprimer',{method:'POST',body:JSON.stringify({lien:b.dataset.lien})});charger()});
}
const p=new URLSearchParams(location.search);if(p.get('lien')||p.get('text')){$('texte').value=[p.get('text'),p.get('lien')].filter(Boolean).join(' ');chercher()}
charger();
</script></body></html>"""


def main():
    code = config()["studio_code"]
    ip = adresse_locale()
    serveur = ThreadingHTTPServer(("0.0.0.0", PORT), Studio)
    print("Le Studio de La Centrale est ouvert.")
    print(f"  Sur ce PC         : http://127.0.0.1:{PORT}")
    print(f"  Téléphone/tablette: http://{ip}:{PORT}   (même Wi-Fi)")
    print(f"  Code d'accès      : {code}")
    print("Laissez cette fenêtre ouverte. Ctrl+C pour arrêter.")
    serveur.serve_forever()


if __name__ == "__main__":
    main()
