"""La Centrale - agrégateur de sources.

Lit la liste des sources dans sources.json, récupère leurs derniers articles
(flux RSS / Atom / YouTube) et génère la page index.html, puis l'ouvre.
Usage : python centrale.py   (ou double-clic sur lancer.bat)
"""
import html
import json
import re
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from telephone import en_articles, infos_page, lire_envois, relever_envois

DOSSIER = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_PAR_SOURCE = 15
LONGUEUR_RESUME = 260


def telecharger(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def texte_brut(fragment):
    """Retire les balises HTML et raccourcit le texte."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment or "", flags=re.S | re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > LONGUEUR_RESUME:
        t = t[:LONGUEUR_RESUME].rsplit(" ", 1)[0] + "…"
    return t


def lire_date(valeur):
    if not valeur:
        return None
    valeur = valeur.strip()
    try:
        d = parsedate_to_datetime(valeur)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(valeur.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def nom_local(tag):
    return tag.rsplit("}", 1)[-1]


def enfants(element, nom):
    return [e for e in element if nom_local(e.tag) == nom]


def premier_texte(element, *noms):
    for nom in noms:
        for e in enfants(element, nom):
            if e.text and e.text.strip():
                return e.text.strip()
    return ""


def trouver_image(item, contenu):
    for e in item.iter():
        n = nom_local(e.tag)
        if n in ("thumbnail", "content") and e.get("url") and (e.get("medium") in (None, "image") or n == "thumbnail"):
            if n == "thumbnail" or re.search(r"\.(jpe?g|png|webp|gif)", e.get("url"), re.I):
                return e.get("url")
        if n == "enclosure" and (e.get("type") or "").startswith("image"):
            return e.get("url")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', contenu or "", re.I)
    return html.unescape(m.group(1)) if m else ""


def analyser_flux(donnees):
    racine = ET.fromstring(donnees)
    items = [e for e in racine.iter() if nom_local(e.tag) in ("item", "entry")]
    articles = []
    for it in items[:MAX_PAR_SOURCE]:
        titre = premier_texte(it, "title")
        lien = premier_texte(it, "link")
        if not lien:  # Atom : <link href="..." rel="alternate"/>
            liens = enfants(it, "link")
            alt = [l for l in liens if l.get("rel") in (None, "alternate")] or liens
            lien = alt[0].get("href", "") if alt else ""
        contenu = premier_texte(it, "description", "summary", "encoded", "content")
        if not contenu:  # YouTube : media:group/media:description
            for e in it.iter():
                if nom_local(e.tag) == "description" and e.text:
                    contenu = e.text
                    break
        date = lire_date(premier_texte(it, "pubDate", "published", "updated", "date"))
        origine = next((e.get("url", "") for e in enfants(it, "source")), "")
        if any(b in origine for b in SITES_BLOQUES_EN_EUROPE):
            continue
        if titre and lien:
            articles.append({
                "titre": texte_brut(titre),
                "lien": lien.strip(),
                "resume": texte_brut(contenu),
                "image": trouver_image(it, contenu),
                "date": date,
            })
    return articles


# Sites qui refusent les visiteurs européens (erreur 451 « RGPD ») : inutile de les proposer.
SITES_BLOQUES_EN_EUROPE = ["eagletribune.com", "chicagotribune.com", "latimes.com", "nydailynews.com",
                           "baltimoresun.com", "orlandosentinel.com", "sun-sentinel.com", "courant.com",
                           "dailypress.com", "mcall.com", "pilotonline.com", "newsday.com", "stltoday.com",
                           "buffalonews.com", "richmond.com", "omaha.com", "tulsaworld.com", "arkansasonline.com"]

UNITES = {"minute": 60, "heure": 3600, "jour": 86400, "semaine": 604800, "mois": 2592000, "an": 31536000}


def date_relative(texte):
    """« il y a 3 jours » -> date approximative."""
    m = re.search(r"il y a (\d+)\s*(minute|heure|jour|semaine|mois|an)", texte or "")
    if not m:
        return None
    secondes = int(m.group(1)) * UNITES[m.group(2)]
    return datetime.fromtimestamp(datetime.now().timestamp() - secondes, timezone.utc)


def videos_youtube(id_chaine):
    """Secours quand le flux RSS de YouTube ne répond pas : lit la page « Vidéos » de la chaîne."""
    page = telecharger(f"https://www.youtube.com/channel/{id_chaine}/videos").decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        raise ValueError("page YouTube illisible")
    blocs = []

    def parcourir(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:
                blocs.append(o["lockupViewModel"])
            for v in o.values():
                parcourir(v)
        elif isinstance(o, list):
            for v in o:
                parcourir(v)

    parcourir(json.loads(m.group(1)))
    articles = []
    for b in blocs[:MAX_PAR_SOURCE]:
        try:
            meta = b["metadata"]["lockupMetadataViewModel"]
            textes = [p["text"]["content"] for r in meta["metadata"]["contentMetadataViewModel"]["metadataRows"]
                      for p in r.get("metadataParts", []) if "text" in p]
            images = b["contentImage"]["thumbnailViewModel"]["image"]["sources"]
        except (KeyError, TypeError):
            continue
        articles.append({
            "titre": meta["title"]["content"],
            "lien": f"https://www.youtube.com/watch?v={b['contentId']}",
            "resume": " · ".join(textes),
            "image": images[-1]["url"] if images else "",
            "date": next((d for d in map(date_relative, textes) if d), None),
        })
    return articles


# Onglets par région : mots-clés cherchés dans le titre et le résumé.
# Un mot terminé par * est un début de mot (« palestin* » trouve palestinien, Palestine…).
REGIONS = {
    "Belgique": ["belgique", "belge*", "bruxell*", "wallon*", "flandre", "flamand*", "de wever", "liège", "anvers",
                 "namur", "charleroi", "gand", "mons", "rtbf", "belgium", "belgian"],
    "France": ["france", "français*", "francais*", "macron", "élysée", "matignon", "assemblée nationale", "sénat",
               "lecornu", "bayrou", "le pen", "mélenchon", "bardella", "paris", "marseille", "lyon", "insee",
               "cour des comptes", "french"],
    "Europe": ["europ*", "ue", "bruxelles", "von der leyen", "allemand*", "allemagne", "berlin", "merz", "italie",
               "italien*", "meloni", "espagne", "pologne", "polonais*", "hongrie", "orban", "royaume-uni", "britanni*",
               "londres", "pays-bas", "néerlandais*", "suisse", "ukrain*", "zelensky", "kiev", "kyiv", "russ*",
               "poutine", "putin", "moscou", "kremlin", "otan", "nato", "bce", "germany", "dutch", "netherlands"],
    "USA": ["états-unis", "etats-unis", "usa", "américain*", "americain*", "amérique", "trump", "washington",
            "maison blanche", "pentagone", "biden", "vance", "new york", "cia", "fbi", "fda", "cdc", "rfk",
            "kennedy", "america*", "u.s.", "congress"],
    "Asie": ["asie", "asiat*", "chin*", "pékin", "beijing", "xi jinping", "japon", "japonais*", "japan", "inde",
             "indien*", "india", "modi", "corée", "korea", "taïwan", "taiwan", "vietnam", "indonésie", "philippines",
             "pakistan", "afghanistan", "kazakhstan", "brics"],
    "Moyen-Orient": ["moyen-orient", "israël*", "israel*", "gaza", "gazaoui*", "palestin*", "cisjordanie", "liban*",
                     "hezbollah", "hamas", "iran*", "téhéran", "syri*", "irak*", "yémen*", "houthi*", "arabie",
                     "saoudi*", "qatar*", "netanyahou", "netanyahu", "turquie", "turc*", "erdogan", "jordanie",
                     "égypte", "middle east"],
    "Monde": ["afrique", "africain*", "amérique latine", "brésil*", "venezuela*", "argentin*", "mexique", "cuba",
              "onu", "oms", "mondial*", "planète", "international*", "géopoliti*", "multipolaire", "sahel", "mali"],
}
_MOTIFS_REGIONS = {
    r: re.compile(r"\b(?:" + "|".join(re.escape(m[:-1]) + r"\w*" if m.endswith("*") else re.escape(m) + r"\b"
                                       for m in mots) + ")", re.I)
    for r, mots in REGIONS.items()
}


# Détection de la langue : on compte les petits mots les plus fréquents de chaque langue.
MOTS_LANGUE = {
    "fr": {"le", "la", "les", "des", "est", "et", "une", "du", "pour", "dans", "que", "qui", "sur", "pas", "au", "avec", "ce", "sont", "par", "aux"},
    "en": {"the", "and", "is", "of", "to", "in", "for", "that", "with", "on", "are", "was", "by", "from", "this", "it", "be", "as", "at", "an"},
    "nl": {"de", "het", "een", "en", "van", "is", "dat", "op", "voor", "met", "niet", "zijn", "aan", "bij", "ook", "naar", "om", "wordt", "werd", "kwam"},
}


def langue_de(article):
    mots = re.findall(r"[a-zà-ÿ]+", f'{article["titre"]} {article["resume"]}'.lower())
    scores = {l: sum(m in liste for m in mots) for l, liste in MOTS_LANGUE.items()}
    meilleure = max(scores, key=scores.get)
    return meilleure if scores[meilleure] else "fr"


def regions_de(article):
    texte = f'{article["titre"]} {article["resume"]}'
    trouvees = [r for r, motif in _MOTIFS_REGIONS.items() if motif.search(texte)]
    # tout ce qui sort de la France et de la Belgique compte aussi pour « Monde »
    if any(r not in ("France", "Belgique", "Monde") for r in trouvees) and "Monde" not in trouvees:
        trouvees.append("Monde")
    return trouvees


def recherche_youtube(termes):
    """Pour une personne sans chaîne à elle : ses dernières vidéos (interviews…) trouvées par recherche YouTube."""
    q = urllib.parse.quote_plus(f'"{termes}"')
    page = telecharger(f"https://www.youtube.com/results?search_query={q}&sp=CAI%253D").decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        raise ValueError("page YouTube illisible")
    videos = []

    def parcourir(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                videos.append(o["videoRenderer"])
            for v in o.values():
                parcourir(v)
        elif isinstance(o, list):
            for v in o:
                parcourir(v)

    parcourir(json.loads(m.group(1)))
    articles = []
    for v in videos[:MAX_PAR_SOURCE]:
        try:
            titre = "".join(r["text"] for r in v["title"]["runs"])
        except KeyError:
            continue
        chaine = "".join(r["text"] for r in v.get("ownerText", {}).get("runs", []))
        quand = v.get("publishedTimeText", {}).get("simpleText", "")
        vignettes = v.get("thumbnail", {}).get("thumbnails", [])
        articles.append({
            "titre": titre,
            "lien": f"https://www.youtube.com/watch?v={v['videoId']}",
            "resume": " · ".join(x for x in (f"Vidéo de {chaine}" if chaine else "", quand) if x),
            "image": vignettes[-1]["url"] if vignettes else "",
            "date": date_relative(quand),
        })
    return articles


def articles_de_page(url, motif):
    """Pour un site sans flux RSS : les liens de la page dont l'adresse contient « motif »."""
    page = telecharger(url).decode("utf-8", "replace")
    liens = []
    for href in re.findall(r'href="([^"#]+)"', page):
        complet = urllib.parse.urljoin(url, html.unescape(href))
        if (motif in complet and complet.rstrip("/") != url.rstrip("/") and complet not in liens
                and not re.search(r"/page[:/]\d+", complet)):
            liens.append(complet)

    def lire(lien):
        try:
            i = infos_page(lien)
        except Exception:
            return None
        return {"titre": texte_brut(re.sub(r"\s+[|–-]\s+[^|–-]+$", "", i["titre"])), "lien": lien, "resume": texte_brut(i["resume"]),
                "image": i["image"], "date": lire_date(i.get("publie_le"))}

    with ThreadPoolExecutor(max_workers=5) as pool:
        return [a for a in pool.map(lire, liens[:MAX_PAR_SOURCE]) if a]


def recuperer(source):
    try:
        if "recherche_youtube" in source:
            articles = recherche_youtube(source["recherche_youtube"])
        elif "page" in source:
            articles = articles_de_page(source["page"], source["motif"])
        else:
            try:
                articles = analyser_flux(telecharger(source["flux"]))
            except Exception:
                chaine = re.search(r"youtube\.com/feeds/videos\.xml\?channel_id=([\w-]+)", source["flux"])
                if not chaine:
                    raise
                articles = videos_youtube(chaine.group(1))
        for a in articles:
            a["source"] = source["nom"]
            a["prudence"] = source.get("prudence", False)
            a["avertir"] = source.get("avertir", False)
            a["theme"] = source.get("theme", "Divers")
        return source, articles, None
    except Exception as e:  # une source en panne ne doit pas bloquer les autres
        return source, [], str(e)


def date_affichee(d):
    if not d:
        return ""
    d = d.astimezone()
    mois = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
    return f"{d.day} {mois[d.month - 1]} {d.year} · {d:%H:%M}"


# Partage : publication sur un canal Telegram (via un bot) et visuels Instagram.
# Le jeton du bot reste dans le navigateur (localStorage), jamais dans index.html.
PARTAGE = r'''
<style>
dialog{border:1px solid var(--ligne);border-radius:10px;background:var(--carte);color:var(--texte);max-width:min(560px,94vw);padding:22px}
dialog::backdrop{background:rgba(0,0,0,.55)}
dialog h2{margin-bottom:14px}
dialog label{display:block;font-size:13px;color:var(--doux);margin:12px 0 4px}
dialog input,dialog textarea{width:100%;padding:8px 10px;border:1px solid var(--ligne);border-radius:6px;background:var(--fond);color:var(--texte);font:inherit}
dialog textarea{min-height:150px;font-size:13px}
dialog .boutons{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;margin-top:16px}
dialog button{font:inherit;cursor:pointer;border:1px solid var(--ligne);background:var(--fond);color:var(--texte);border-radius:6px;padding:7px 14px}
dialog button.principal{background:var(--accent);border-color:var(--accent);color:#fff}
dialog .aide{font-size:13px;color:var(--doux);margin:0}
#apercu-insta{width:100%;border-radius:6px;display:block;border:1px solid var(--ligne)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--texte);color:var(--fond);padding:10px 18px;border-radius:8px;z-index:50;font-size:14px;max-width:90vw}
</style>
<dialog id="reglages">
  <h2>Réglages</h2>
  <p class="aide">Pour Telegram : créez un canal, créez un bot avec @BotFather, puis ajoutez le bot comme administrateur du canal.</p>
  <label for="tg-jeton">Jeton du bot Telegram (donné par @BotFather)</label>
  <input id="tg-jeton" placeholder="123456789:AAH...">
  <label for="tg-canal">Nom du canal Telegram</label>
  <input id="tg-canal" placeholder="@lacentrale_info">
  <label for="insta-compte">Votre compte Instagram (affiché sur les visuels)</label>
  <input id="insta-compte" placeholder="@lacentrale">
  <div class="boutons"><button id="tg-test">Tester Telegram</button><button class="principal" id="reglages-ok">Enregistrer</button></div>
</dialog>
<dialog id="dlg-insta">
  <h2>Visuel Instagram</h2>
  <img id="apercu-insta" alt="Aperçu du visuel">
  <label for="legende-insta">Texte de la publication (à coller dans Instagram)</label>
  <textarea id="legende-insta"></textarea>
  <div class="boutons"><button id="insta-fermer">Fermer</button><button id="insta-copier">Copier le texte</button><button class="principal" id="insta-telecharger">Télécharger l'image</button></div>
</dialog>
<div id="toast" hidden></div>
<script>
(function(){
const lire=(k,d)=>{try{return localStorage.getItem(k)??d}catch(e){return d}};
const ecrire=(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}};
const $=id=>document.getElementById(id);
function toast(t){const e=$('toast');e.textContent=t;e.hidden=false;clearTimeout(toast.m);toast.m=setTimeout(()=>e.hidden=true,4000)}
function infos(carte){return{titre:carte.querySelector('h3').textContent,resume:carte.querySelector('p').textContent,
  source:carte.dataset.source,theme:carte.dataset.theme,lien:carte.dataset.lien}}
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const motCle=s=>'#'+s.normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/[^A-Za-z0-9]/g,'');

/* Réglages */
$('ouvrir-reglages').onclick=()=>{$('tg-jeton').value=lire('tg-jeton','');$('tg-canal').value=lire('tg-canal','');
  $('insta-compte').value=lire('insta-compte','');$('reglages').showModal()};
$('reglages-ok').onclick=()=>{ecrire('tg-jeton',$('tg-jeton').value.trim());ecrire('tg-canal',$('tg-canal').value.trim());
  ecrire('insta-compte',$('insta-compte').value.trim());$('reglages').close();toast('Réglages enregistrés')};

/* Telegram : requête « simple » (formulaire) pour passer depuis un fichier local */
async function telegram(methode,params){
  const jeton=lire('tg-jeton',''),canal=lire('tg-canal','');
  if(!jeton||!canal){$('ouvrir-reglages').click();throw new Error('Renseignez d\'abord le bot et le canal')}
  const r=await fetch(`https://api.telegram.org/bot${jeton}/${methode}`,{method:'POST',body:new URLSearchParams({chat_id:canal,...params})});
  const j=await r.json();if(!j.ok)throw new Error(j.description||'erreur Telegram');return j;
}
$('tg-test').onclick=async()=>{ecrire('tg-jeton',$('tg-jeton').value.trim());ecrire('tg-canal',$('tg-canal').value.trim());
  try{await telegram('sendMessage',{text:'✅ La Centrale est bien connectée à ce canal.'});toast('Message de test envoyé sur le canal')}
  catch(e){toast('Échec : '+e.message)}};
let envoyes=new Set();try{envoyes=new Set(JSON.parse(lire('centrale-envoyes','[]')))}catch(e){}
function majEnvois(){document.querySelectorAll('.btn-telegram').forEach(b=>{const on=envoyes.has(b.closest('.carte').dataset.lien);
  b.classList.toggle('envoye',on);b.title=on?'Déjà publié sur Telegram (cliquer pour republier)':'Publier sur le canal Telegram'})}
document.querySelectorAll('.btn-telegram').forEach(b=>b.onclick=async()=>{
  const a=infos(b.closest('.carte'));
  const texte=`<b>${esc(a.titre)}</b>\n\n${esc(a.resume)}\n\n📰 ${esc(a.source)} · ${motCle(a.theme)}\n🔗 ${esc(a.lien)}`;
  b.disabled=true;
  try{await telegram('sendMessage',{text:texte,parse_mode:'HTML'});envoyes.add(a.lien);ecrire('centrale-envoyes',JSON.stringify([...envoyes]));
    majEnvois();toast('Publié sur Telegram')}
  catch(e){toast('Échec : '+e.message)}
  b.disabled=false;
});
majEnvois();
/* sources déjà condamnées en justice : lecture seule, pas de partage */
document.querySelectorAll('.carte[data-prudence] .btn-telegram, .carte[data-prudence] .btn-insta').forEach(b=>{
  b.disabled=true;b.title='Partage désactivé : source déjà condamnée en justice';b.style.opacity='.3';b.style.cursor='not-allowed'});
/* sources condamnées par le passé pour certains contenus : partage possible après vérification */
document.querySelectorAll('.carte[data-avertir] .btn-telegram, .carte[data-avertir] .btn-insta').forEach(b=>{
  b.title+=' (à vérifier avant de partager)';
  b.addEventListener('click',ev=>{if(!confirm('Ce site a déjà été condamné pour certains contenus. Avez-vous vérifié que cet article ne pose pas de problème ?')){ev.stopImmediatePropagation();ev.preventDefault()}},true)});

/* Instagram : visuel 1080×1350 dessiné dans le navigateur */
function lignes(ctx,texte,largeur){const mots=texte.split(' '),res=[];let l='';
  for(const m of mots){const t=l?l+' '+m:m;if(ctx.measureText(t).width>largeur&&l){res.push(l);l=m}else l=t}
  if(l)res.push(l);return res}
function visuel(a){
  const c=document.createElement('canvas');c.width=1080;c.height=1350;const x=c.getContext('2d');
  x.fillStyle='#16150f';x.fillRect(0,0,1080,1350);
  x.fillStyle='#b3261e';x.fillRect(0,0,1080,14);x.fillRect(80,1180,920,4);
  x.fillStyle='#ece7dd';x.font='800 64px Georgia,serif';x.textBaseline='top';
  x.fillText('LA ',80,90);const w=x.measureText('LA ').width;x.fillStyle='#ff6b5e';x.fillText('CENTRALE',80+w,90);
  x.font='600 30px system-ui,sans-serif';const th=a.theme.toUpperCase();const tw=x.measureText(th).width;
  x.fillStyle='#b3261e';x.fillRect(80,200,tw+36,52);x.fillStyle='#fff';x.fillText(th,98,210);
  let taille=78,ls;do{x.font=`700 ${taille}px Georgia,serif`;ls=lignes(x,a.titre,920);taille-=4}while(ls.length*taille*1.25>700&&taille>40);
  x.fillStyle='#ece7dd';let y=310;for(const l of ls){x.fillText(l,80,y);y+=(taille+4)*1.25}
  x.font='italic 40px Georgia,serif';x.fillStyle='#a39b8d';x.fillText('Source : '+a.source,80,Math.min(y+40,1100));
  x.font='600 34px system-ui,sans-serif';x.fillStyle='#ece7dd';x.fillText('Lien en bio',80,1215);
  const compte=lire('insta-compte','');if(compte){x.textAlign='right';x.fillStyle='#ff6b5e';x.fillText(compte,1000,1215);x.textAlign='left'}
  return c.toDataURL('image/png');
}
let imageCourante='',nomCourant='';
document.querySelectorAll('.btn-insta').forEach(b=>b.onclick=()=>{
  const a=infos(b.closest('.carte'));imageCourante=visuel(a);
  nomCourant='la-centrale-'+a.titre.toLowerCase().normalize('NFD').replace(/[^a-z0-9]+/g,'-').slice(0,50)+'.png';
  $('apercu-insta').src=imageCourante;
  $('legende-insta').value=`${a.titre}\n\n${a.resume}\n\n📰 Source : ${a.source}\n🔗 Lien vers l'article en bio\n\n#LaCentrale ${motCle(a.theme)} #reinformation #decryptage`;
  $('dlg-insta').showModal();
});
$('insta-fermer').onclick=()=>$('dlg-insta').close();
$('insta-telecharger').onclick=()=>{const l=document.createElement('a');l.href=imageCourante;l.download=nomCourant;l.click()};
$('insta-copier').onclick=async()=>{const t=$('legende-insta');
  try{await navigator.clipboard.writeText(t.value)}catch(e){t.select();document.execCommand('copy')}toast('Texte copié')};
})();
</script>
'''


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def nom_du_jour(d, aujourdhui):
    if not d:
        return "Date inconnue"
    j = d.astimezone().date()
    ecart = (aujourdhui - j).days
    if ecart <= 0:
        return "Aujourd'hui"
    if ecart == 1:
        return "Hier"
    return f"{JOURS[j.weekday()].capitalize()} {j.day} {MOIS[j.month - 1]}"


def generer_page(articles, etat_sources):
    e = html.escape
    maintenant = datetime.now().astimezone()
    themes = sorted({a["theme"] for a in articles})
    ts = lambda a: int(a["date"].timestamp()) if a["date"] else 0
    recents = sum(1 for a in articles if a["date"] and (maintenant - a["date"]).total_seconds() < 86400)

    # carrousel : l'article illustré le plus récent de chaque source (8 au maximum)
    diapos, vues = [], set()
    for a in articles:
        if a["image"] and a["source"] not in vues:
            diapos.append(a)
            vues.add(a["source"])
        if len(diapos) == 8:
            break

    pris = {a["lien"] for a in diapos}
    nos_publications = [a for a in articles if a.get("perso")][:6]
    mosaique, par_source = [], {}
    for a in articles:  # 2 articles maximum par source pour varier
        if a["image"] and a["lien"] not in pris and par_source.get(a["source"], 0) < 2:
            mosaique.append(a)
            par_source[a["source"]] = par_source.get(a["source"], 0) + 1
        if len(mosaique) == 60:
            break

    def tuile(a):
        return f'''<a class="tuile" data-langue="{a.get("langue", "fr")}" href="{e(a["lien"])}" target="_blank" rel="noopener">
  <img src="{e(a["image"])}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">
  <span class="tsrc">{e(a["source"])}</span><span class="tt">{e(a["titre"])}</span></a>'''

    def carte(a):
        img = f'<img src="{e(a["image"])}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' if a["image"] else ""
        ancre = f' id="{e(a["lien"][1:])}"' if a["lien"].startswith("#") else ""
        return f'''<article class="carte"{ancre} data-source="{e(a["source"])}" data-theme="{e(a["theme"])}" data-lien="{e(a["lien"])}" data-ts="{ts(a)}" data-langue="{a.get("langue", "fr")}" data-regions="{e("|".join(a.get("regions", [])))}"{' data-prudence="1"' if a.get("prudence") else ""}{' data-avertir="1"' if a.get("avertir") else ""}>
  <a class="lien" href="{e(a["lien"])}" target="_blank" rel="noopener">{img}
    <div class="corps">
      <div class="meta"><span class="nouveau" hidden>Nouveau</span><span class="src">{e(a["source"])}</span><span class="theme">{e(a["theme"])}</span><time class="rel" data-ts="{ts(a)}" title="{date_affichee(a["date"])}">{date_affichee(a["date"])}</time></div>
      <h3>{e(a["titre"])}</h3>
      <p>{e(a["resume"])}</p>
    </div>
  </a>
  <div class="actions">
    <button class="etoile" title="Ajouter à ma sélection" aria-label="Ajouter à ma sélection">☆</button>
    <button class="btn-telegram" title="Publier sur le canal Telegram" aria-label="Publier sur Telegram">✈</button>
    <button class="btn-insta" title="Créer un visuel Instagram" aria-label="Créer un visuel Instagram">▣</button>
  </div>
</article>'''

    def diapo(a, i):
        return f'''<a class="diapo{" active" if i == 0 else ""}" data-langue="{a.get("langue", "fr")}" href="{e(a["lien"])}" target="_blank" rel="noopener">
  <img src="{e(a["image"])}" alt="" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">
  <div class="voile"><div class="meta"><span class="src">{e(a["source"])}</span><span class="theme">{e(a["theme"])}</span><time class="rel" data-ts="{ts(a)}"></time></div>
  <h3>{e(a["titre"])}</h3><p>{e(a["resume"])}</p></div></a>'''

    # fil groupé par jour
    fil, jour_courant = [], None
    aujourdhui = maintenant.date()
    for a in articles:
        jour = nom_du_jour(a["date"], aujourdhui)
        if jour != jour_courant:
            fil.append(f'<div class="jour">{e(jour)}</div>')
            jour_courant = jour
        fil.append(carte(a))

    bandeau = "".join(
        f'<a data-langue="{a.get("langue", "fr")}" href="{e(a["lien"])}" target="_blank" rel="noopener"><b>{e(a["source"])}</b> {e(a["titre"])}</a>'
        for a in articles[:60]
    )
    derniere_minute = "".join(
        f'<li data-langue="{a.get("langue", "fr")}"><a href="{e(a["lien"])}" target="_blank" rel="noopener"><time class="rel" data-ts="{ts(a)}"></time>'
        f'<span class="src">{e(a["source"])}</span><span class="t">{e(a["titre"])}</span></a></li>'
        for a in articles[:30]
    )
    liste_sources = "".join(
        f'<li><button class="filtre-source" data-source="{e(s["nom"])}">{e(s["nom"])}</button>'
        f'<span class="{"ok" if not err else "ko"}" title="{e(err or "")}">{n if not err else "hors ligne"}</span></li>'
        for s, n, err in etat_sources
    )
    # dossiers documentés (dossiers.json) : ce qui est établi / ce qui reste débattu
    try:
        dossiers = json.loads((DOSSIER / "dossiers.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        dossiers = []
    bloc_dossiers = "".join(
        f'''<article class="dossier"><h3>{e(d["titre"])}</h3>
  <p><span class="etiq etabli">Établi</span>{e(d["etabli"])}</p>
  <p><span class="etiq debattu">Débattu</span>{e(d["debattu"])}</p>
  <ul>{"".join(f'<li><a href="{e(u)}" target="_blank" rel="noopener">{e(t)}</a></li>' for t, u in d["liens"])}</ul></article>'''
        for d in dossiers
    )
    onglets = "".join(f'<button class="onglet" data-region="{r}">{r}</button>' for r in REGIONS)
    boutons_themes = "".join(f'<button class="filtre-theme" data-theme="{e(t)}">{e(t)}</button>' for t in themes)
    duree_bandeau = max(40, 7 * min(len(articles), 25))

    return f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>La Centrale</title>
<style>
:root{{--fond:#f6f3ee;--carte:#fff;--texte:#1d1b18;--doux:#6b645a;--ligne:#e2dcd2;--accent:#b3261e;--accent-doux:#f6dcd9;--nuit:#16150f;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--fond:#16150f;--carte:#211f19;--texte:#ece7dd;--doux:#a39b8d;--ligne:#36322a;--accent:#ff6b5e;--accent-doux:#3a1e1b;--nuit:#0b0a07;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--fond);color:var(--texte);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:var(--nuit);color:#ece7dd;padding:10px 20px;display:flex;align-items:center;justify-content:center;gap:8px 24px;flex-wrap:wrap;text-align:center}}
header h1{{margin:0;font:800 clamp(28px,4vw,42px)/1 Georgia,"Times New Roman",serif;letter-spacing:.04em;text-transform:uppercase}}
header h1 span{{color:#ff6b5e}}
header p{{margin:0;opacity:.85;font-size:14px;font-style:italic;font-family:Georgia,serif}}
header p.maj{{font:12px system-ui,sans-serif;opacity:.75;margin-top:4px}}
.onglets{{background:var(--nuit);display:flex;justify-content:center;flex-wrap:wrap;gap:2px;padding:0 12px;border-top:1px solid #333}}
.onglet{{font:700 14px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:#cfc8bb;background:none;border:0;border-bottom:3px solid transparent;padding:12px 14px;cursor:pointer}}
.onglet:hover{{color:#fff}}
.onglet.actif{{color:#fff;border-bottom-color:#ff6b5e}}
.dossiers{{max-width:1240px;margin:28px auto 0;padding:0 20px}}
.grille-dossiers{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:14px}}
.dossier{{background:var(--carte);border:1px solid var(--ligne);border-left:4px solid var(--accent);border-radius:8px;padding:16px 18px}}
.dossier h3{{font:700 19px/1.3 Georgia,serif;margin:0 0 10px}}
.dossier p{{margin:0 0 8px;font-size:14px}}
.etiq{{display:inline-block;font:800 10px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;border-radius:3px;padding:2px 6px;margin-right:8px;vertical-align:1px}}
.etiq.etabli{{background:#1f7a4d;color:#fff}}.etiq.debattu{{background:#b7791f;color:#fff}}
.dossier ul{{margin:8px 0 0;padding-left:18px;font-size:13px}}
.dossier a{{color:var(--accent)}}
.nos-pub{{max-width:1240px;margin:20px auto 0;padding:0 20px}}
.grille-pub{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:12px}}
.grille-pub .carte{{border:2px solid var(--accent)}}
.grille-pub .carte .lien{{display:block}}.grille-pub .carte img{{width:100%;aspect-ratio:16/9}}
.mosaique{{min-width:0;height:100%}}
.grille{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:repeat(3,minmax(0,1fr));gap:6px;height:100%}}
.tuile{{position:relative;min-height:0;border-radius:6px;overflow:hidden;background:var(--nuit);color:#fff;text-decoration:none;display:block}}
.tuile img{{width:100%;height:100%;object-fit:cover;transition:transform .4s}}
.tuile:hover img{{transform:scale(1.08)}}
.tuile::after{{content:"";position:absolute;inset:0;background:linear-gradient(transparent 30%,rgba(0,0,0,.9))}}
.tsrc,.tt{{position:absolute;left:9px;right:9px;z-index:1}}
.tsrc{{bottom:auto;top:8px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;background:#b3261e;border-radius:3px;padding:1px 5px;left:8px;right:auto}}
.tt{{bottom:7px;font:700 12px/1.22 Georgia,serif;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
header .compteur{{display:inline-block;background:#b3261e;color:#fff;border-radius:20px;padding:1px 10px;font-weight:600;margin-right:6px}}
.bandeau{{display:flex;align-items:stretch;background:var(--carte);border-bottom:1px solid var(--ligne);overflow:hidden;font-size:14px}}
.bandeau .direct{{flex:none;background:var(--accent);color:#fff;font-weight:800;letter-spacing:.08em;padding:9px 14px;display:flex;align-items:center;gap:8px;z-index:1}}
.bandeau .direct::before{{content:"";width:8px;height:8px;border-radius:50%;background:#fff;animation:pulse 1.4s infinite}}
@keyframes pulse{{50%{{opacity:.2}}}}
.defile{{overflow:hidden;flex:1;display:flex;align-items:center}}
.piste{{display:flex;white-space:nowrap;animation:defile {duree_bandeau}s linear infinite}}
.bandeau:hover .piste{{animation-play-state:paused}}
.piste a{{color:var(--texte);text-decoration:none;padding:0 26px;border-right:1px solid var(--ligne)}}
.piste a b{{color:var(--accent);margin-right:6px}}
.piste a:hover{{text-decoration:underline}}
@keyframes defile{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.barre{{position:sticky;top:0;z-index:5;background:var(--fond);border-bottom:1px solid var(--ligne);padding:8px 20px;display:flex;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin;gap:8px;align-items:center}}
.barre>*{{flex:none}}
.barre input{{width:220px}}
.barre input{{padding:8px 12px;border:1px solid var(--ligne);border-radius:6px;background:var(--carte);color:var(--texte);font:inherit}}
.barre button,.sources button{{font:inherit;cursor:pointer;border:1px solid var(--ligne);background:var(--carte);color:var(--texte);border-radius:20px;padding:5px 12px}}
.barre select{{font:inherit;border:1px solid var(--ligne);background:var(--carte);color:var(--texte);border-radius:20px;padding:5px 10px;cursor:pointer}}
.barre button.actif{{background:var(--accent);border-color:var(--accent);color:#fff}}
.haut{{max-width:1320px;margin:0 auto;padding:14px 20px 0;display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1.2fr) minmax(0,.95fr);gap:12px;height:clamp(380px,calc(100vh - 220px),540px)}}
.haut>*{{min-height:0}}
@media (max-width:1100px){{.haut{{grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);height:auto}}.carrousel{{height:420px}}.minute{{height:420px}}
  .mosaique{{grid-column:1/-1;order:3}}.grille{{grid-template-columns:repeat(6,minmax(0,1fr));grid-template-rows:none}}.tuile{{aspect-ratio:1}}}}
@media (max-width:700px){{.haut{{grid-template-columns:minmax(0,1fr);padding:10px 12px 0}}.carrousel{{height:auto;aspect-ratio:4/3}}.minute{{height:auto;max-height:420px}}
  .grille{{grid-template-columns:repeat(3,minmax(0,1fr))}}header .sous{{min-width:0;max-width:100%}}}}
.carrousel{{position:relative;border-radius:10px;overflow:hidden;background:var(--nuit);height:100%}}
.diapo{{position:absolute;inset:0;opacity:0;transition:opacity .8s;color:#fff;text-decoration:none;pointer-events:none}}
.diapo.active{{opacity:1;pointer-events:auto}}
.diapo img{{width:100%;height:100%;object-fit:cover;transform:scale(1.04);transition:transform 7s linear}}
.diapo.active img{{transform:scale(1.12)}}
.voile{{position:absolute;inset:auto 0 0 0;padding:50px 20px 22px;background:linear-gradient(transparent,rgba(0,0,0,.88))}}
.voile h3{{font:700 clamp(18px,1.9vw,26px)/1.2 Georgia,serif;margin:6px 0 8px;text-shadow:0 2px 8px rgba(0,0,0,.5)}}
.voile p{{margin:0;opacity:.85;font-size:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.voile .meta{{color:#ddd}}.voile .src{{color:#ff8a7f}}.voile .theme{{background:rgba(255,255,255,.15)}}
.fleche{{position:absolute;top:32%;transform:translateY(-50%);z-index:2;width:42px;height:42px;border-radius:50%;border:0;background:rgba(0,0,0,.45);color:#fff;font-size:24px;cursor:pointer}}
.fleche:hover{{background:var(--accent)}}
.fleche.g{{left:12px}}.fleche.d{{right:12px}}
.points{{position:absolute;top:14px;right:16px;display:flex;gap:6px;z-index:2}}
.points button{{width:10px;height:10px;border-radius:50%;border:0;background:rgba(255,255,255,.45);cursor:pointer;padding:0}}
.points button.active{{background:#fff;width:24px;border-radius:5px}}
.progression{{position:absolute;left:0;bottom:0;height:4px;background:var(--accent);z-index:2;width:0}}
.progression.court{{animation:progres 6s linear forwards}}
@keyframes progres{{to{{width:100%}}}}
.minute{{background:var(--carte);border:1px solid var(--ligne);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;height:100%}}
.minute h2{{margin-bottom:6px}}
.minute ul{{list-style:none;margin:0;padding:0;overflow:auto}}
.minute li a{{display:grid;grid-template-columns:62px 1fr;gap:2px 10px;padding:8px 0;border-bottom:1px solid var(--ligne);color:var(--texte);text-decoration:none}}
.minute li:last-child a{{border:0}}
.minute time{{grid-row:span 2;font-size:12px;font-weight:700;color:var(--accent)}}
.minute .src{{font-size:12px}}
.minute .t{{font:600 14px/1.3 Georgia,serif}}
.minute a:hover .t{{color:var(--accent)}}
main{{max-width:1240px;margin:0 auto;padding:24px 20px;display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:28px}}
@media (max-width:900px){{main{{grid-template-columns:minmax(0,1fr)}}}}
h2{{font:700 13px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:8px;margin:0 0 16px}}
.fil{{display:flex;flex-direction:column;gap:12px}}
.jour{{font:700 20px Georgia,serif;margin:14px 0 2px;display:flex;align-items:center;gap:12px}}
.jour::after{{content:"";flex:1;height:1px;background:var(--ligne)}}
.fil .jour:first-child{{margin-top:0}}
.carte{{position:relative;background:var(--carte);border:1px solid var(--ligne);border-radius:8px;overflow:hidden;animation:apparait .5s both}}
@keyframes apparait{{from{{opacity:0;transform:translateY(8px)}}}}
.carte .lien{{color:inherit;text-decoration:none;display:flex;gap:14px;height:100%}}
.carte img{{width:190px;flex:none;aspect-ratio:4/3;object-fit:cover;display:block;background:var(--ligne)}}
@media (max-width:560px){{.carte .lien{{display:block}}.carte img{{width:100%}}}}
.corps{{padding:12px 44px 14px 14px}}
.carte h3{{font:700 17px/1.3 Georgia,serif;margin:4px 0 6px}}
.carte:hover h3{{color:var(--accent)}}
.carte p{{margin:0;color:var(--doux);font-size:14px;overflow-wrap:anywhere}}
.meta{{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--doux);align-items:center}}
.src{{font-weight:700;color:var(--accent)}}
.theme{{background:var(--accent-doux);border-radius:3px;padding:0 6px}}
.nouveau{{background:var(--accent);color:#fff;border-radius:3px;padding:0 6px;font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.05em}}
.actions{{position:absolute;top:8px;right:8px;display:flex;flex-direction:column;gap:6px}}
.actions button{{width:30px;height:30px;border-radius:50%;border:1px solid var(--ligne);background:var(--carte);color:var(--doux);font-size:15px;cursor:pointer;padding:0}}
.actions button:hover{{color:var(--accent);border-color:var(--accent)}}
.etoile.on,.btn-telegram.envoye{{color:#fff!important;background:var(--accent)!important;border-color:var(--accent)!important}}
aside .bloc{{background:var(--carte);border:1px solid var(--ligne);border-radius:8px;padding:16px;margin-bottom:20px}}
.sources{{list-style:none;margin:0;padding:0}}
.sources li{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--ligne)}}
.sources li:last-child{{border:0}}
.sources button{{border:0;padding:0;background:none;text-align:left;border-radius:0}}
.sources button:hover{{color:var(--accent)}}
.ok{{font-size:12px;color:var(--doux)}}.ko{{font-size:11px;color:var(--accent)}}
.vide{{color:var(--doux);padding:30px;text-align:center}}
#remonter{{position:fixed;right:18px;bottom:18px;width:44px;height:44px;border-radius:50%;border:0;background:var(--accent);color:#fff;font-size:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:6}}
footer{{text-align:center;color:var(--doux);font-size:12px;padding:30px 20px}}
@media (prefers-reduced-motion:reduce){{.piste,.carte,.progression.court{{animation:none}}.diapo,.diapo img{{transition:none}}}}
[hidden]{{display:none!important}}
</style></head><body>
<header><h1>La <span>Centrale</span></h1><div class="sous"><p>Pour une démocratie participative · toutes les sources au même endroit · sans étiquette</p>
<p class="maj"><span class="compteur">{recents} nouvelles publications en 24 h</span> Mis à jour le {date_affichee(maintenant)}</p></div></header>
<div class="bandeau"><span class="direct">EN DIRECT</span><div class="defile"><div class="piste">{bandeau}{bandeau}</div></div></div>
<nav class="onglets"><button class="onglet actif" data-region="">À la une</button>{onglets}</nav>
<div class="barre">
  <input id="recherche" type="search" placeholder="Rechercher un mot, un sujet…">
  <button class="filtre-theme actif" data-theme="">Tout</button>{boutons_themes}
  <select id="langue" aria-label="Langue des articles"><option value="">🌐 Toutes les langues</option><option value="fr">Français</option><option value="en">English</option><option value="nl">Nederlands</option></select>
  <button id="ma-selection">★ Ma sélection</button>
  <button id="ouvrir-reglages" title="Réglages Telegram / Instagram">⚙ Réglages</button>
</div>
<section class="haut" id="bloc-une">
  <div class="carrousel" id="carrousel">
    {"".join(diapo(a, i) for i, a in enumerate(diapos))}
    <button class="fleche g" aria-label="Précédent">‹</button><button class="fleche d" aria-label="Suivant">›</button>
    <div class="points">{"".join(f'<button aria-label="Article {i + 1}"></button>' for i in range(len(diapos)))}</div>
    <div class="progression"></div>
  </div>
  <div class="mosaique" id="bloc-mosaique"><div class="grille">{"".join(tuile(a) for a in mosaique)}</div></div>
  <div class="minute"><h2>Dernière minute</h2><ul>{derniere_minute}</ul></div>
</section>
<section class="nos-pub" id="bloc-nos-pub"{"" if nos_publications else " hidden"}><h2>Nos publications</h2><div class="grille-pub">{"".join(carte(a) for a in nos_publications)}</div></section>
<section class="dossiers" id="bloc-dossiers"{" hidden" if not dossiers else ""}><h2>Dossiers documentés · les faits et leurs sources</h2><div class="grille-dossiers">{bloc_dossiers}</div></section>
<main>
  <section><h2 id="titre-fil">Toutes les publications</h2><div class="fil" id="fil">{"".join(fil)}</div>
  <p class="vide" id="vide" hidden>Aucun article ne correspond.</p></section>
  <aside>
    <div class="bloc"><h2>Les sources</h2><ul class="sources">{liste_sources}</ul></div>
    <div class="bloc"><h2>Notre ligne</h2><p style="margin:0 0 10px;color:var(--doux);font-size:14px"><b style="color:var(--texte)">La démocratie participative.</b> Les citoyens n'ont pas besoin d'étiquettes ni d'intermédiaires pour penser : ils ont besoin d'accès aux faits, aux documents et à tous les points de vue, pour décider eux-mêmes.</p>
    <p style="margin:0;color:var(--doux);font-size:14px">Chaque titre renvoie vers l'article d'origine. Lisez, comparez, remontez aux documents : à vous de juger. ☆ garde un article dans votre sélection, ✈ le publie sur Telegram, ▣ crée un visuel Instagram.</p></div>
  </aside>
</main>
<button id="remonter" hidden aria-label="Remonter en haut">↑</button>
<footer>La Centrale · {len(articles)} articles de {sum(1 for _, _, err in etat_sources if not err)} sources</footer>
<script>
const cartes=[...document.querySelectorAll('#fil .carte')];
const jours=[...document.querySelectorAll('#fil .jour')];
const etoiles=document.querySelectorAll('.etoile');
let theme='',source='',selection=false,region='';
let langue='';try{{langue=localStorage.getItem('centrale-langue')||''}}catch(e){{}}
document.getElementById('langue').value=langue;
let favoris=new Set();
try{{favoris=new Set(JSON.parse(localStorage.getItem('centrale-favoris')||'[]'))}}catch(e){{}}
function sauver(){{try{{localStorage.setItem('centrale-favoris',JSON.stringify([...favoris]))}}catch(e){{}}}}
function majEtoiles(){{etoiles.forEach(b=>{{const on=favoris.has(b.closest('.carte').dataset.lien);b.classList.toggle('on',on);b.textContent=on?'★':'☆'}})}}

/* heures relatives (« il y a 2 h »), recalculées chaque minute */
function relatif(ts){{
  if(!+ts)return'';const s=Date.now()/1000-ts;
  if(s<60)return"à l'instant";if(s<3600)return'il y a '+Math.floor(s/60)+' min';
  if(s<86400)return'il y a '+Math.floor(s/3600)+' h';const j=Math.floor(s/86400);return j===1?'hier':'il y a '+j+' j';
}}
function majHeures(){{
  document.querySelectorAll('time.rel').forEach(t=>{{const r=relatif(t.dataset.ts);if(r)t.textContent=r}});
  cartes.forEach(c=>{{const s=Date.now()/1000-c.dataset.ts;c.querySelector('.nouveau').hidden=!(+c.dataset.ts&&s<86400)}});
}}
majHeures();setInterval(majHeures,60000);

/* carrousel */
const diapos=[...document.querySelectorAll('.diapo')],points=[...document.querySelectorAll('.points button')],barre=document.querySelector('.progression');
let idx=0,pos=0,minuteur=null,visibles=diapos.map((_,k)=>k);
function montrer(i){{
  if(!visibles.length)return;pos=(i+visibles.length)%visibles.length;idx=visibles[pos];
  diapos.forEach((d,k)=>d.classList.toggle('active',k===idx));points.forEach((p,k)=>p.classList.toggle('active',k===idx));
  barre.classList.remove('court');void barre.offsetWidth;barre.classList.add('court');
}}
function lancer(){{clearInterval(minuteur);minuteur=setInterval(()=>montrer(pos+1),6000);montrer(pos)}}
function pause(){{clearInterval(minuteur);barre.classList.remove('court')}}
document.querySelector('.fleche.g').onclick=()=>{{montrer(pos-1);lancer()}};
document.querySelector('.fleche.d').onclick=()=>{{montrer(pos+1);lancer()}};
points.forEach((p,k)=>p.onclick=()=>{{montrer(visibles.indexOf(k));lancer()}});
const car=document.getElementById('carrousel');car.onmouseenter=pause;car.onmouseleave=lancer;
/* haut de page limité à la langue choisie */
function majLangueHaut(){{
  visibles=diapos.map((_,k)=>k).filter(k=>!langue||diapos[k].dataset.langue===langue);
  points.forEach((p,k)=>p.hidden=!visibles.includes(k));
  document.querySelectorAll('.tuile,.minute li,.piste a').forEach(x=>x.hidden=!!langue&&x.dataset.langue!==langue);
  const maxTuiles=matchMedia('(min-width:1101px)').matches?9:12;
  document.querySelectorAll('.tuile:not([hidden])').forEach((x,k)=>x.hidden=k>=maxTuiles);
  document.querySelectorAll('.minute li:not([hidden])').forEach((x,k)=>x.hidden=k>=15);
  car.hidden=!visibles.length;pos=0;if(visibles.length)lancer();
}}
majLangueHaut();

function filtrer(){{
  const q=document.getElementById('recherche').value.toLowerCase().trim();
  let n=0;
  cartes.forEach(c=>{{
    const ok=(!langue||c.dataset.langue===langue)&&(!region||c.dataset.regions.split('|').includes(region))&&(!theme||c.dataset.theme===theme)&&(!source||c.dataset.source===source)
      &&(!selection||favoris.has(c.dataset.lien))&&(!q||c.textContent.toLowerCase().includes(q));
    c.hidden=!ok;if(ok)n++;
  }});
  jours.forEach(j=>{{let el=j.nextElementSibling,vu=false;
    while(el&&!el.classList.contains('jour')){{if(!el.hidden){{vu=true;break}}el=el.nextElementSibling}}j.hidden=!vu}});
  document.getElementById('vide').hidden=n>0;
  const filtre=!!(region||theme||source||selection||q);
  document.getElementById('bloc-une').hidden=filtre;document.getElementById('bloc-mosaique').hidden=filtre;
  if(document.querySelector('#bloc-nos-pub .carte'))document.getElementById('bloc-nos-pub').hidden=filtre;
  if(document.getElementById('bloc-dossiers').children.length>1)document.getElementById('bloc-dossiers').hidden=filtre;filtre?pause():lancer();
  document.getElementById('titre-fil').textContent=[region,selection?'Ma sélection':source||theme].filter(Boolean).join(' · ')||'Toutes les publications';
}}
document.querySelectorAll('.filtre-theme').forEach(b=>b.onclick=()=>{{
  theme=b.dataset.theme;source='';selection=false;
  document.querySelectorAll('.barre button').forEach(x=>x.classList.toggle('actif',x===b));filtrer()}});
document.querySelectorAll('.filtre-source').forEach(b=>b.onclick=()=>{{
  source=b.dataset.source;theme='';selection=false;
  document.querySelectorAll('.barre button').forEach(x=>x.classList.remove('actif'));filtrer();scrollTo({{top:0,behavior:'smooth'}})}});
document.getElementById('ma-selection').onclick=e=>{{
  selection=!selection;theme='';source='';
  document.querySelectorAll('.barre button').forEach(x=>x.classList.toggle('actif',x===e.target&&selection));filtrer()}};
document.getElementById('recherche').oninput=filtrer;
document.getElementById('langue').onchange=e=>{{langue=e.target.value;try{{localStorage.setItem('centrale-langue',langue)}}catch(x){{}}majLangueHaut();filtrer()}};
document.querySelectorAll('.onglet').forEach(b=>b.onclick=()=>{{region=b.dataset.region;
  document.querySelectorAll('.onglet').forEach(x=>x.classList.toggle('actif',x===b));filtrer()}});
etoiles.forEach(b=>b.onclick=()=>{{const l=b.closest('.carte').dataset.lien;favoris.has(l)?favoris.delete(l):favoris.add(l);sauver();majEtoiles();if(selection)filtrer()}});
majEtoiles();
const haut=document.getElementById('remonter');
addEventListener('scroll',()=>haut.hidden=scrollY<800,{{passive:true}});haut.onclick=()=>scrollTo({{top:0,behavior:'smooth'}});
</script>
''' + PARTAGE + '</body></html>'


CACHE = DOSSIER / "cache.json"


def collecter():
    """Récupère toutes les sources (réseau) et garde le résultat dans cache.json."""
    sources = json.loads((DOSSIER / "sources.json").read_text(encoding="utf-8"))
    print(f"Récupération de {len(sources)} sources…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        resultats = list(pool.map(recuperer, sources))

    articles, etat, deja_vus = [], [], set()
    for source, arts, err in resultats:
        # un même article peut venir de deux sources (ex. Vincent Held et Réseau International)
        arts = [a for a in arts if a["lien"] not in deja_vus]
        deja_vus.update(a["lien"] for a in arts)
        articles.extend(arts)
        etat.append((source, len(arts), err))
        print(f"  {'OK ' if not err else 'ERR'} {source['nom']:<35} {len(arts) if not err else err}")

    CACHE.write_text(json.dumps({
        "articles": [{**x, "date": x["date"].isoformat() if x["date"] else None} for x in articles],
        "etat": etat,
    }, ensure_ascii=False), encoding="utf-8")
    return articles, etat


def construire(articles, etat):
    """Ajoute vos publications (Studio, bot Telegram) aux articles et écrit index.html."""
    articles = list(articles)
    deja_vus = {a["lien"] for a in articles}
    for a in en_articles(lire_envois()):
        if a["lien"] in deja_vus:  # votre publication remplace l'article venu d'un flux
            articles = [x for x in articles if x["lien"] != a["lien"]]
        deja_vus.add(a["lien"])
        articles.append(a)

    maintenant = datetime.now(timezone.utc)
    for a in articles:
        if a["date"] and a["date"] > maintenant:  # date d'un événement à venir, pas de publication
            a["date"] = None
        a["regions"] = regions_de(a)
        a["langue"] = langue_de(a)

    # tri du plus récent au plus ancien (les articles sans date vont à la fin)
    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    page = DOSSIER / "index.html"
    page.write_text(generer_page(articles, etat), encoding="utf-8")
    publique = DOSSIER / "public"  # copie servie par le lien de partage
    if publique.exists():
        (publique / "index.html").write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    return page, len(articles)


def reconstruire():
    """Refait la page sans retélécharger les sources (utilisé par le Studio)."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return construire(*collecter())
    articles = [{**a, "date": datetime.fromisoformat(a["date"]) if a["date"] else None} for a in cache["articles"]]
    return construire(articles, [tuple(e) for e in cache["etat"]])


def main():
    articles, etat = collecter()
    _, statut = relever_envois()  # liens et PDF envoyés au bot Telegram
    print(f"  Téléphone : {statut}")
    page, n = construire(articles, etat)
    print(f"\n{n} articles -> {page}")
    webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
