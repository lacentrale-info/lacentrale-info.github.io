"""La Centrale - agrégateur de sources.

Lit la liste des sources dans sources.json, récupère leurs derniers articles
(flux RSS / Atom / YouTube) et génère la page index.html, puis l'ouvre.
Usage : python centrale.py   (ou double-clic sur lancer.bat)
"""
import html
import json
import re
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

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
        if titre and lien:
            articles.append({
                "titre": texte_brut(titre),
                "lien": lien.strip(),
                "resume": texte_brut(contenu),
                "image": trouver_image(it, contenu),
                "date": date,
            })
    return articles


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


def recuperer(source):
    try:
        try:
            articles = analyser_flux(telecharger(source["flux"]))
        except Exception:
            chaine = re.search(r"youtube\.com/feeds/videos\.xml\?channel_id=([\w-]+)", source["flux"])
            if not chaine:
                raise
            articles = videos_youtube(chaine.group(1))
        for a in articles:
            a["source"] = source["nom"]
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


def generer_page(articles, etat_sources):
    e = html.escape
    maintenant = datetime.now().astimezone()
    themes = sorted({a["theme"] for a in articles})
    une = [a for a in articles if a["image"]][:5]

    def carte(a, classe="carte"):
        img = f'<img src="{e(a["image"])}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' if a["image"] else ""
        return f'''<article class="{classe}" data-source="{e(a["source"])}" data-theme="{e(a["theme"])}" data-lien="{e(a["lien"])}">
  <a class="lien" href="{e(a["lien"])}" target="_blank" rel="noopener">{img}
    <div class="corps">
      <div class="meta"><span class="src">{e(a["source"])}</span><span class="theme">{e(a["theme"])}</span><time>{date_affichee(a["date"])}</time></div>
      <h3>{e(a["titre"])}</h3>
      <p>{e(a["resume"])}</p>
    </div>
  </a>
  <button class="etoile" title="Ajouter à ma sélection" aria-label="Ajouter à ma sélection">☆</button>
</article>'''

    liste_sources = "".join(
        f'<li><button class="filtre-source" data-source="{e(s["nom"])}">{e(s["nom"])}</button>'
        f'<span class="{"ok" if not err else "ko"}" title="{e(err or "")}">{n if not err else "hors ligne"}</span></li>'
        for s, n, err in etat_sources
    )
    boutons_themes = "".join(f'<button class="filtre-theme" data-theme="{e(t)}">{e(t)}</button>' for t in themes)

    return f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>La Centrale</title>
<style>
:root{{--fond:#f6f3ee;--carte:#fff;--texte:#1d1b18;--doux:#6b645a;--ligne:#e2dcd2;--accent:#b3261e;--accent-doux:#f6dcd9;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--fond:#16150f;--carte:#211f19;--texte:#ece7dd;--doux:#a39b8d;--ligne:#36322a;--accent:#ff6b5e;--accent-doux:#3a1e1b;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--fond);color:var(--texte);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:var(--texte);color:var(--fond);padding:22px 20px 18px;text-align:center}}
header h1{{margin:0;font:800 clamp(34px,6vw,58px)/1 Georgia,"Times New Roman",serif;letter-spacing:.04em;text-transform:uppercase}}
header h1 span{{color:var(--accent)}}
header p{{margin:8px 0 0;opacity:.75;font-size:14px}}
.barre{{position:sticky;top:0;z-index:5;background:var(--fond);border-bottom:1px solid var(--ligne);padding:10px 20px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
.barre input{{flex:1 1 220px;padding:8px 12px;border:1px solid var(--ligne);border-radius:6px;background:var(--carte);color:var(--texte);font:inherit}}
.barre button,.sources button{{font:inherit;cursor:pointer;border:1px solid var(--ligne);background:var(--carte);color:var(--texte);border-radius:20px;padding:5px 12px}}
.barre button.actif{{background:var(--accent);border-color:var(--accent);color:#fff}}
main{{max-width:1240px;margin:0 auto;padding:20px;display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:28px}}
@media (max-width:900px){{main{{grid-template-columns:1fr}}}}
h2{{font:700 13px/1 system-ui,sans-serif;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:8px;margin:0 0 16px}}
.une{{display:grid;grid-template-columns:2fr 1fr 1fr;grid-template-rows:auto auto;gap:14px;margin-bottom:34px}}
.une .carte:first-child{{grid-row:span 2}}
.une .carte:first-child h3{{font-size:24px}}
@media (max-width:700px){{.une{{grid-template-columns:1fr}}.une .carte:first-child{{grid-row:auto}}}}
.fil{{display:flex;flex-direction:column;gap:12px}}
.carte{{position:relative;background:var(--carte);border:1px solid var(--ligne);border-radius:8px;overflow:hidden}}
.carte .lien{{color:inherit;text-decoration:none;display:block;height:100%}}
.carte img{{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--ligne)}}
.fil .carte .lien{{display:flex;gap:14px}}
.fil .carte img{{width:190px;flex:none;aspect-ratio:4/3}}
@media (max-width:560px){{.fil .carte .lien{{display:block}}.fil .carte img{{width:100%}}}}
.corps{{padding:12px 44px 14px 14px}}
.carte h3{{font:700 17px/1.3 Georgia,serif;margin:4px 0 6px}}
.carte:hover h3{{color:var(--accent)}}
.carte p{{margin:0;color:var(--doux);font-size:14px}}
.meta{{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--doux)}}
.src{{font-weight:700;color:var(--accent)}}
.theme{{background:var(--accent-doux);border-radius:3px;padding:0 6px}}
.etoile{{position:absolute;top:8px;right:8px;width:30px;height:30px;border-radius:50%;border:1px solid var(--ligne);background:var(--carte);color:var(--doux);font-size:16px;cursor:pointer}}
.etoile.on{{color:#fff;background:var(--accent);border-color:var(--accent)}}
aside .bloc{{background:var(--carte);border:1px solid var(--ligne);border-radius:8px;padding:16px;margin-bottom:20px}}
.sources{{list-style:none;margin:0;padding:0}}
.sources li{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--ligne)}}
.sources li:last-child{{border:0}}
.sources button{{border:0;padding:0;background:none;text-align:left;border-radius:0}}
.sources button:hover{{color:var(--accent)}}
.ok{{font-size:12px;color:var(--doux)}}.ko{{font-size:11px;color:var(--accent)}}
.vide{{color:var(--doux);padding:30px;text-align:center}}
footer{{text-align:center;color:var(--doux);font-size:12px;padding:30px 20px}}
[hidden]{{display:none!important}}
</style></head><body>
<header><h1>La <span>Centrale</span></h1><p>Toutes les sources au même endroit · sans étiquette · mis à jour le {date_affichee(maintenant)}</p></header>
<div class="barre">
  <input id="recherche" type="search" placeholder="Rechercher un mot, un sujet…">
  <button class="filtre-theme actif" data-theme="">Tout</button>{boutons_themes}
  <button id="ma-selection">★ Ma sélection</button>
</div>
<main>
  <div>
    <section id="bloc-une"><h2>À la une</h2><div class="une">{"".join(carte(a) for a in une)}</div></section>
    <section><h2 id="titre-fil">Dernières publications</h2><div class="fil" id="fil">{"".join(carte(a) for a in articles)}</div>
    <p class="vide" id="vide" hidden>Aucun article ne correspond.</p></section>
  </div>
  <aside>
    <div class="bloc"><h2>Les sources</h2><ul class="sources">{liste_sources}</ul></div>
    <div class="bloc"><h2>Le principe</h2><p style="margin:0;color:var(--doux);font-size:14px">Chaque titre renvoie vers l'article d'origine. Lisez, comparez, remontez aux documents : à vous de juger. Cliquez sur ☆ pour garder un article dans votre sélection.</p></div>
  </aside>
</main>
<footer>La Centrale · {len(articles)} articles de {sum(1 for _, _, err in etat_sources if not err)} sources</footer>
<script>
const cartes=[...document.querySelectorAll('#fil .carte')];
const etoiles=document.querySelectorAll('.etoile');
let theme='',source='',selection=false;
let favoris=new Set();
try{{favoris=new Set(JSON.parse(localStorage.getItem('centrale-favoris')||'[]'))}}catch(e){{}}
function sauver(){{try{{localStorage.setItem('centrale-favoris',JSON.stringify([...favoris]))}}catch(e){{}}}}
function majEtoiles(){{etoiles.forEach(b=>{{const on=favoris.has(b.parentElement.dataset.lien);b.classList.toggle('on',on);b.textContent=on?'★':'☆'}})}}
function filtrer(){{
  const q=document.getElementById('recherche').value.toLowerCase().trim();
  let n=0;
  cartes.forEach(c=>{{
    const ok=(!theme||c.dataset.theme===theme)&&(!source||c.dataset.source===source)
      &&(!selection||favoris.has(c.dataset.lien))&&(!q||c.textContent.toLowerCase().includes(q));
    c.hidden=!ok;if(ok)n++;
  }});
  document.getElementById('vide').hidden=n>0;
  document.getElementById('bloc-une').hidden=!!(theme||source||selection||q);
  document.getElementById('titre-fil').textContent=selection?'Ma sélection':source?source:theme?theme:'Dernières publications';
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
etoiles.forEach(b=>b.onclick=()=>{{const l=b.parentElement.dataset.lien;favoris.has(l)?favoris.delete(l):favoris.add(l);sauver();majEtoiles();if(selection)filtrer()}});
majEtoiles();
</script>
</body></html>'''


def main():
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

    # tri du plus récent au plus ancien (les articles sans date vont à la fin)
    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    page = DOSSIER / "index.html"
    page.write_text(generer_page(articles, etat), encoding="utf-8")
    print(f"\n{len(articles)} articles -> {page}")
    webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
