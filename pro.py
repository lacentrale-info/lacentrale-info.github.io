"""Page « La Centrale Pro — bientôt » (pro.html) : présente la future version payante et recueille
l'avis des visiteurs SANS RIEN ENREGISTRER : le formulaire prépare un message dans la messagerie
du visiteur (lien mailto), qu'il relit et envoie lui-même.

Les textes sont en français ; leurs traductions (anglais, espagnol, néerlandais) sont dans traductions.py.
"""
import html

CONTACT = "lacentraleinfo@protonmail.com"  # adresse qui reçoit les avis

FONCTIONS = [  # (emoji, titre, description) — aussi les cases à cocher du formulaire
    ("📡", "Vos propres sources",
     "Ajoutez les sites, chaînes et canaux que vous suivez : ils alimentent vos fils, comme le reste du site. Chaque source passe par la charte."),
    ("⚖️", "Comparer les points de vue",
     "Pour un même événement, voyez côte à côte comment chaque source le raconte. Le meilleur moyen de se faire son propre avis."),
    ("☀️", "Le point du jour",
     "Chaque matin, par mail ou sur Telegram, l'essentiel de vos fils. Aussi en version audio, à écouter comme un podcast."),
    ("🌐", "Traduire et résumer",
     "Lisez dans votre langue un article publié dans une autre, ou son résumé rapide. Toujours signalé comme automatique, avec le lien vers l'original."),
    ("🔔", "Alertes",
     "Soyez prévenu dès qu'un sujet qui vous tient à cœur apparaît dans une source."),
    ("🗄️", "Archives et dossiers personnels",
     "Cherchez dans tout l'historique, rangez vos articles avec vos notes, exportez-les en PDF."),
    ("🔄", "Sur tous vos appareils",
     "Vos fils, vos sources et votre sélection vous suivent du téléphone à l'ordinateur."),
    ("🏘️", "Votre propre Centrale",
     "Pour une association, un collectif ou un journaliste : votre version du site, avec vos sources et votre nom, sous la même charte."),
]

PRINCIPES = [
    ("🆓", "L'essentiel reste gratuit",
     "Tout ce que le site propose aujourd'hui reste libre et gratuit, pour toujours. La version Pro ajoute des outils, jamais des barrières."),
    ("🛡️", "Une charte stricte",
     "Aucune source appartenant à un milliardaire ou à un grand groupe, aucun média de propagande d'État."),
    ("🤝", "Rétribuer l'information libre",
     "Une partie de chaque abonnement est reversée aux médias indépendants que La Centrale relaie."),
]

CHARTE = [
    "Pas de média appartenant à un milliardaire ou à un grand groupe industriel ou financier.",
    "Pas de média d'État servant de propagande.",
    "Pas de site condamné pour incitation à la haine.",
    "Des sources variées, de tous les horizons : chacun se fait son avis, sans étiquette.",
]

PRIX = ["4 € / mois", "40 € / an", "Un tarif plus bas", "Je ne paierais pas"]


def generer_pro(i18n_tete, langues_ui):
    e = html.escape
    choix_ui = "".join(f'<option value="{c}">{c.upper()} · {n}</option>' for c, n in langues_ui.items())
    principes = "".join(f'<div class="principe"><div class="ico">{i}</div><h3 data-t>{e(t)}</h3><p data-t>{e(d)}</p></div>'
                        for i, t, d in PRINCIPES)
    fonctions = "".join(f'<div class="fonction"><div class="ico">{i}</div><h3 data-t>{e(t)}</h3><p data-t>{e(d)}</p></div>'
                        for i, t, d in FONCTIONS)
    charte = "".join(f"<li data-t>{e(c)}</li>" for c in CHARTE)
    cases = "".join(f'<label class="case"><input type="checkbox" name="fonction" value="{e(t)}"> {i} <span data-t>{e(t)}</span></label>'
                    for i, t, d in FONCTIONS)
    prix = "".join(f'<label class="case"><input type="radio" name="prix" value="{e(p)}"> <span data-t>{e(p)}</span></label>' for p in PRIX)
    return f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{i18n_tete}
<title>La Centrale Pro</title>
<style>
:root{{--fond:#f6f3ee;--carte:#fff;--texte:#1d1b18;--doux:#6b645a;--ligne:#e2dcd2;--accent:#b3261e;--accent-doux:#f6dcd9;--nuit:#16150f;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--fond:#16150f;--carte:#211f19;--texte:#ece7dd;--doux:#a39b8d;--ligne:#36322a;--accent:#ff6b5e;--accent-doux:#3a1e1b;--nuit:#0b0a07;color-scheme:dark}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--fond);color:var(--texte);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:var(--nuit);color:#ece7dd;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px}}
.logo{{font:800 26px Georgia,serif;letter-spacing:.04em;text-transform:uppercase;color:#ece7dd;text-decoration:none}}.logo span{{color:#ff6b5e}}
.choix-ui{{font:600 12px system-ui,sans-serif;color:#ece7dd;background:#211f19;border:1px solid #555;border-radius:20px;padding:4px 8px}}
main{{max-width:1040px;margin:0 auto;padding:0 20px 40px}}
.hero{{text-align:center;padding:56px 0 36px}}
.sur{{display:inline-block;background:var(--accent);color:#fff;font:800 12px system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase;border-radius:20px;padding:4px 14px;margin:0}}
.hero h1{{font:800 clamp(38px,7vw,64px)/1.05 Georgia,serif;margin:16px 0 14px}}.hero h1 span{{color:var(--accent)}}
.accroche{{font-size:clamp(17px,2.2vw,20px);color:var(--doux);max-width:640px;margin:0 auto 26px}}
.bouton{{display:inline-block;background:var(--accent);color:#fff;font:700 16px system-ui,sans-serif;text-decoration:none;border:0;border-radius:28px;padding:13px 28px;cursor:pointer}}
.bouton:hover{{filter:brightness(1.08)}}
h2{{font:800 clamp(24px,3.4vw,32px)/1.2 Georgia,serif;margin:56px 0 18px;text-align:center}}
.grille{{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(min(100%,230px),1fr))}}
.grille-3{{grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr))}}
.principe,.fonction{{background:var(--carte);border:1px solid var(--ligne);border-radius:12px;padding:18px}}
.principe{{border-top:4px solid var(--accent)}}
.ico{{font-size:28px;line-height:1}}
.principe h3,.fonction h3{{font:700 18px/1.3 Georgia,serif;margin:10px 0 6px}}
.principe p,.fonction p{{margin:0;color:var(--doux);font-size:14.5px}}
.charte{{background:var(--carte);border:1px solid var(--ligne);border-left:5px solid var(--accent);border-radius:12px;padding:6px 24px 18px;max-width:760px;margin:0 auto}}
.charte li{{margin:8px 0}}
.prix{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}}
.tarif{{background:var(--carte);border:1px solid var(--ligne);border-radius:12px;padding:18px 28px;text-align:center;min-width:180px}}
.tarif b{{display:block;font:800 30px Georgia,serif;color:var(--accent)}}
.note{{text-align:center;color:var(--doux);font-size:14px;margin:12px 0 0}}
#avis form{{background:var(--carte);border:1px solid var(--ligne);border-radius:14px;padding:22px;max-width:760px;margin:0 auto}}
#avis h3{{font:700 16px system-ui,sans-serif;margin:16px 0 8px}}#avis h3:first-child{{margin-top:0}}
.cases{{display:grid;gap:6px;grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),1fr))}}
.case{{display:flex;gap:8px;align-items:center;padding:8px 10px;border:1px solid var(--ligne);border-radius:10px;cursor:pointer;font-size:15px}}
.case:has(input:checked){{border-color:var(--accent);background:var(--accent-doux)}}
.case input{{width:18px;height:18px;accent-color:var(--accent)}}
textarea{{width:100%;min-height:90px;padding:10px 12px;border:1px solid var(--ligne);border-radius:10px;background:var(--fond);color:var(--texte);font:inherit}}
.aide{{color:var(--doux);font-size:14px}}
#avis .bouton{{width:100%;margin-top:16px}}
footer{{text-align:center;padding:30px 20px;color:var(--doux);font-size:14px}}footer a{{color:var(--accent)}}
</style></head><body>
<header><a class="logo" href="index.html">La <span>Centrale</span></a>
<select id="choix-ui" class="choix-ui" aria-label="Langue du site" data-ta>{choix_ui}</select></header>
<main>
<section class="hero"><p class="sur" data-t>Bientôt</p><h1>La Centrale <span>Pro</span></h1>
<p class="accroche" data-t>L'information reste gratuite pour tout le monde. La version Pro ajoute des outils pour aller plus loin, et finance les médias indépendants.</p>
<a class="bouton" href="#avis" data-t>Donner mon avis</a></section>
<section><div class="grille grille-3">{principes}</div></section>
<h2 data-t>Ce que proposerait la version Pro</h2>
<section><div class="grille">{fonctions}</div></section>
<h2 data-t>La charte des sources</h2>
<section class="charte"><p data-t>Chaque source ajoutée par un abonné est vérifiée. Une source qui plaît à beaucoup peut être proposée pour le site public : les membres votent.</p><ul>{charte}</ul></section>
<h2 data-t>Prix envisagé</h2>
<section><div class="prix"><div class="tarif"><b>4 €</b><span data-t>par mois</span></div><div class="tarif"><b>40 €</b><span data-t>par an</span></div></div>
<p class="note" data-t>Un tarif solidaire est prévu. Prix indicatif : rien n'est encore payant, votre avis nous aide à décider.</p></section>
<section id="avis"><h2 data-t>Ça vous intéresse ?</h2>
<form id="form-avis">
  <h3 data-t>Ce qui vous intéresse le plus</h3><div class="cases">{cases}</div>
  <h3 data-t>Quel prix vous semble juste ?</h3><div class="cases">{prix}</div>
  <h3 data-t>Une idée, une remarque ? (facultatif)</h3><textarea id="remarque"></textarea>
  <button class="bouton" type="submit" data-t>✉ Préparer mon message</button>
  <p class="aide" data-t>Rien n'est enregistré par ce site : le message se prépare dans votre propre messagerie, vous le relisez avant de l'envoyer.</p>
</form></section>
</main>
<footer><a href="index.html" data-t>← Retour à La Centrale</a></footer>
<script>
traduirePage();
{{const s=document.getElementById('choix-ui');s.value=UI;s.onchange=()=>{{try{{localStorage.setItem('centrale-ui',s.value)}}catch(e){{}}location.reload()}}}}
document.getElementById('form-avis').onsubmit=ev=>{{
  ev.preventDefault();
  const coches=[...document.querySelectorAll('input[name=fonction]:checked')].map(x=>'- '+t(x.value));
  const prix=document.querySelector('input[name=prix]:checked'),rq=document.getElementById('remarque').value.trim();
  const corps=[t('Ce qui m’intéresse :'),...(coches.length?coches:['-']),'',t('Prix :')+' '+(prix?t(prix.value):'-'),
    ...(rq?['',t('Remarque :'),rq]:[]),'','('+UI.toUpperCase()+')'].join('\\n');
  location.href='mailto:{CONTACT}?subject='+encodeURIComponent(t('La Centrale Pro : mon avis'))+'&body='+encodeURIComponent(corps);
}};
</script>
</body></html>'''
