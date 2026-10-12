# patchnames

Rename and archive photo/video files using EXIF metadata.

**Canonical filename format:** `YYMMDD_HHMMSS[_WHO]_FILENUM.ext`

```
260429_143022_EA_0234.nef
260429_143022_0234.jpg
```

## Installation

```bash
pip install git+https://github.com/eartigau/patchnames.git
```

Then create your config file:

```bash
patchnames init-config
# Edit ~/.config/patchnames/config.json to match your setup
```

## Dependencies

- [`exiftool`](https://exiftool.org) must be on your PATH: `brew install exiftool`
- Adobe DNG Converter (optional, only needed for `nef-to-dng`)

## Usage

```bash
# Rename + archive all photos in the current directory
patchnames

# Convert Nikon Z50 II NEF → DNG
patchnames nef-to-dng

# Fix timestamps (camera was in wrong timezone)
patchnames time-offset '*.NEF' +=02:06:00
patchnames time-offset '*.NEF' -=00:30:00
patchnames time-offset '*.NEF'      # prompts interactively

# Force a date on files with no EXIF date
patchnames patchdate '*.jpg'        # prompts for YYMMDD

# The same, with every photo described by Claude and the birds identified
# with your eBird checklists, and the page to search them (see below; that
# part is in French, like everything patchdoc says)
patchdoc
patchdoc gui
```


## Keywords with Claude

`patchnames keywords` describes the bird photos of the current directory
with Claude and writes the result into the files themselves (EXIF, IPTC and
XMP): a title, the species (French and Latin names), the habitat, a short
description in French, and a Lightroom hierarchy (Espèce, Espèce latine,
Habitat). An uncertain identification gets the keyword
`identification à confirmer`, with the candidates in the description.

```bash
patchnames keywords              # the bird photos not yet described
patchnames keywords --dry-run    # show Claude's answers, write nothing
patchnames --keywords            # describe, then rename and archive as usual
```

- **What is looked at:** only the photos taken with a bird lens (config
  `bird_lenses`; `--all-lenses` for all), and not the ones that already have a
  title (`--force` to redo them).
- **Sequences:** the photos are grouped into sequences (a new one after 30 s
  without a shot, `--gap`). Claude looks at the middle frame of each sequence,
  at a crop of it at full resolution around the camera's focus point (where a
  distant bird is large enough to identify; the centre of the frame when the
  file has no focus data) and at a contact sheet of all its frames, and its
  answer is written into every frame: a burst of a hundred frames costs one
  call.
- **Your explanations:** `~/.config/patchnames/keywords.md` (written on the
  first run, yours to edit) is passed to Claude as it is: region, naming
  conventions (French names of the Québec list, Latin names of eBird/Clements),
  how prudent to be, habitat words, description style.
- **Needs** the `claude` command (Claude Code) logged in: it runs in print
  mode, allowed only to read the images (`--model` to pick a model). About 20 s
  per sequence.
- **Backups:** exiftool keeps each original as `<file>_original`
  (`--no-backup` to skip; `exiftool -delete_original .` removes them later).
- Set `"keywords_auto": true` in the config to describe before every
  `patchnames` run.

## patchdoc : chaque photo décrite, les oiseaux identifiés avec eBird

(Cette section est en français, comme tout ce que `patchdoc` affiche.)

`patchdoc` fait ce que fait `patchnames` (renommer, archiver) et, au passage,
demande à Claude de décrire chaque photo. Une photo prise au-delà de 200 mm
est regardée comme une photo d'oiseau : Claude essaie de nommer l'espèce,
avec vos listes eBird comme a priori. Le résultat est écrit dans les
fichiers eux-mêmes et dans un index, que `patchdoc gui` permet de fouiller
par espèce, lieu, date, sujet ou n'importe quel mot.

```bash
cd /chemin/des/nouvelles/photos
patchdoc --dry-run     # essai : montrer ce que dit Claude, ne rien changer
patchdoc               # décrire, écrire dans les fichiers, renommer, archiver, indexer
patchdoc gui           # la page de recherche, dans le navigateur
```

Comment cela marche :

- **Séquences.** Les photos sont groupées en séquences (une nouvelle après
  30 s sans prise de vue, `--gap`), les photos d'oiseaux (focale au-delà de
  `bird_focal_min`, 200 mm ; `--min-focal`) à part des autres. Un appel à
  Claude par séquence, quelle que soit sa longueur, trois à la fois
  (`--jobs`), environ 15 s chacun.
- **eBird comme a priori.** La plupart des photos d'oiseaux sont prises
  pendant qu'une liste est remplie. La liste dont les heures contiennent une
  séquence donne son lieu et les espèces notées ; Claude reçoit cette liste
  (et les autres listes du jour) avec la photo. C'est un a priori, pas la
  réponse : Claude sait que la liste peut être fausse ou incomplète, nomme
  ce qu'il voit, et dit en une phrase quand la photo ne s'accorde pas avec
  la liste. Une séquence dont l'espèce n'est pas sur sa liste est signalée
  (`absent de la liste eBird`), une identification incertaine reçoit
  `identification à confirmer`.
- **Une horloge d'appareil décalée.** Un appareil resté à l'heure d'un autre
  pays, ou du mauvais côté d'un changement d'heure, est décalé d'un nombre
  entier d'heures. Pour chaque jour, le décalage qui place le plus de
  séquences dans des listes est cherché, appliqué, et annoncé au début du
  passage. Il doit s'accorder avec le fuseau que l'appareil enregistre (un
  Nikon Z le fait) : un appareil à l'heure de Montréal en Suède a 6 h de
  retard, pas 3. Un appareil qui n'enregistre pas de fuseau ne reçoit qu'une
  correction qui tient sur plusieurs jours ; `--clock 6` la fixe vous-même
  (`--clock 0` pour aucune).
- **Ce qui est écrit dans chaque fichier** (comme `patchnames keywords`,
  avec exiftool ; `--no-backup` pour ne pas garder les copies
  `<fichier>_original`) : titre, description, mots-clés (espèce en français
  et en latin, habitat, sujet), une hiérarchie Lightroom (Espèce, Espèce
  latine, Habitat, Sujet), le lieu (lieu, comté, province, pays), la liste
  comme événement IPTC (`eBird S123456789`), et la latitude et la longitude
  du lieu eBird de la liste. Cette position est celle du lieu, pas celle du
  photographe : `GPSAreaInformation` le dit (`eBird : nom du lieu`). Une
  photo qui porte déjà sa propre position (téléphone, appareil avec GPS) la
  garde, et son lieu est nommé d'après cette position plutôt que d'après la
  liste (voir « Les lieux des photos de cellulaire ») ; la liste reste son
  événement.
- **Vos explications** sont le même fichier que pour `patchnames keywords` :
  `~/.config/patchnames/keywords.md` (`--instructions` pour un autre).
- Une séquence que Claude n'a pas pu décrire reste dans le dossier :
  relancez `patchdoc`. Les photos qui ont déjà un titre ne sont pas décrites
  de nouveau (`--force`), seulement indexées. `--index-only` décrit et
  indexe sans toucher aux fichiers.

### Les notes d'une photo d'oiseau, et la meilleure vue d'une rafale

Une photo d'oiseau reçoit trois notes de 1 à 5 (5 est le mieux) : la netteté
de la mise au point sur l'œil, à quel point l'oiseau est dégagé, et à quel
point la photo est prise à la hauteur de son œil. Elles sont écrites dans le
fichier en mots-clés (`œil net 4/5`) et dans une hiérarchie Lightroom
(`Qualité|Œil net|4`), et servent de filtres dans la page de recherche.

Dans une rafale, chaque vue a ses propres notes, pour trouver la meilleure
du lot :

- `patchdoc` lit chaque vue en pleine résolution.
- **Premier regard de Claude** : toutes les vues, recadrées autour de leur
  point de mise au point et réduites (16 par planche, 96 vues au plus ;
  au-delà, une vue prend les notes de sa voisine). Il dit pour chaque vue si
  l'œil se voit et où il est, le dégagement et la hauteur d'œil.
- **Second regard**, un appel court : l'œil de chaque vue en pleine
  résolution, un carré de 512 pixels centré là où le premier regard l'a
  trouvé. Le point de mise au point de l'appareil ne suffit pas pour cela :
  il n'est pas sur l'œil, et la tête bouge d'une vue à l'autre. S'il y a
  plus de douze vues où l'œil se voit, ce sont les douze dont l'œil mesure
  le plus net. Claude note la netteté de chaque œil, avec pour indice la
  netteté mesurée dans chaque carré.
- Une vue dont l'œil se voit sans être parmi ces douze reçoit la note de la
  vue jugée dont la netteté mesurée est la plus proche, moins un point
  chaque fois que la sienne est deux fois moindre. Une vue où l'œil ne se
  voit pas n'a pas de note d'œil ; une vue que l'oiseau a quittée n'en a
  aucune.
- La meilleure vue est celle dont le total est le plus haut, l'œil comptant
  double. Elle porte le mot-clé `meilleure vue` (`Qualité|Meilleure vue`)
  et c'est elle qui représente la séquence dans la page : la tuile, les
  filtres par note et la visionneuse s'ouvrent sur elle.
- Dans la visionneuse, chaque vue de la bande porte ses trois notes (œil,
  dégagé, hauteur ; un point quand il n'y en a pas) et une étoile marque la
  meilleure ; les notes affichées suivent la vue à l'écran.

Ce qu'il faut en savoir :

- La netteté mesurée l'est dans le carré de l'œil : elle se compare d'une
  vue à l'autre d'une même rafale, pas d'une rafale à l'autre. Elle choisit
  les vues du second regard, lui sert d'indice et départage deux vues à
  égalité ; la note, c'est Claude qui la donne en regardant l'œil.
- Le titre, la description et les mots-clés valent pour la séquence : ils
  sont les mêmes dans toutes ses vues.
- Une séquence décrite avant cette version garde pour toutes ses vues les
  notes de sa vue du milieu ; « Identifier de nouveau avec Claude » la note
  vue par vue.
- Une rafale montre plus d'images à Claude qu'une séquence ordinaire (voir
  « Ce que cela consomme »).

### Les panoramas

Une séquence prise en tournant l'appareil, chaque vue recouvrant la
suivante, est assemblée en une seule image. `patchdoc` la reconnaît sur
votre machine (les vues avancent toutes dans le même sens, d'une bonne
partie du cadre mais pas de tout) : une rafale de la même vue n'est pas un
panorama. L'assemblage est fait par OpenCV, sur des vues réduites à
`pano_size` pixels (3000 ; 0 pour la pleine taille, lent), et le panorama
est un JPEG à côté de ses vues, daté comme la première, dont le modèle
d'appareil porte « (panorama) ». Il est décrit, renommé, archivé et indexé
comme une photo de plus. Dans la visionneuse, « Assembler le panorama » le
fait pour une séquence déjà archivée.

### Ce que cela consomme

Seules les photos du dossier courant sont montrées à Claude, et la
séquence pour laquelle vous cliquez « Identifier la séquence avec Claude »
dans la page de recherche ; `patchdoc index`, `patchdoc vignettes` et le
bouton « Écrire dans les fichiers » ne l'appellent jamais.
`patchdoc` passe par la commande `claude` (votre compte Claude Code) : avec
un abonnement, rien n'est facturé à l'appel, mais chaque séquence compte
dans vos limites d'utilisation. Mesuré sur quatre séquences, en dollars US
au prix de l'API :

| réglage | par séquence | réponses |
|---|---|---|
| modèle par défaut de `claude` (Opus 5.5) | 0,10 $ | référence |
| `--effort low` | 0,08 $ | les mêmes |
| `--model sonnet --effort low` | 0,05 $ | les mêmes |
| `--model haiku --effort low` | 0,003 $ | les mêmes espèces, moins sûr de lui, descriptions moins justes |

Une rafale d'oiseau coûte davantage, parce que chaque vue est montrée et
qu'il y a deux appels : mesuré avec le modèle par défaut sur une rafale de
7 vues, 0,21 $ pour le premier regard et 0,06 $ pour le second (toujours
demandé à effort réduit : ses notes sont sorties les mêmes). Avec
`--model haiku --effort low`, 0,006 $ en tout, mais il juge moins bien
l'œil. `{"doc_eyes_model": "sonnet"}` confie le seul second regard à
Sonnet : mêmes notes à l'essai, pour 0,02 à 0,04 $.

Chaque passage annonce ce qu'il compte dépenser. Il n'y a pas de plafond
par défaut ; pour en mettre un, `doc_budget` (en dollars au prix de l'API)
ou `--budget` : le passage, ou la file de la page, s'y arrête, et les
séquences restantes attendent le suivant. Pour changer le réglage une fois
pour toutes, dans `~/.config/patchnames/config.json` :

```json
{"keywords_model": "sonnet", "doc_effort": "low", "doc_budget": 5}
```

### Les données eBird

`patchdoc` utilise le plus récent export « Download My Data » qu'il trouve :
un `ebird_*.zip` de `~/Downloads` (config `ebird_search`) est copié dans
`~/.config/patchnames/ebird/`. Quand les photos sont plus récentes que
l'export, un nouvel export est demandé avant de décrire (`--no-refresh` pour
s'en passer, `--refresh` pour le forcer) :

```bash
patchdoc ebird             # les données sur disque, les comptes connus
patchdoc ebird --login     # garder les comptes eBird et courriel dans le trousseau de macOS
patchdoc ebird --refresh   # demander un nouvel export à eBird et l'attendre
```

eBird envoie le lien d'un export par courriel, quelques minutes après la
demande. Avec le compte eBird dans le trousseau, la demande est faite pour
vous ; sans lui, la page eBird s'ouvre dans le navigateur et vous cliquez
sur « Request My Observations ». Avec le compte de la boîte courriel dans
le trousseau (IMAP ; Proton Mail Bridge sur `127.0.0.1:1143` par défaut,
config `imap_host` et `imap_port`), le lien est lu dans le courriel et le
zip téléchargé ; sans lui, ouvrez le lien vous-même : le zip est pris dès
qu'il arrive dans `~/Downloads`. Ctrl-C met fin à l'attente et le passage
continue avec les données sur disque. Les mots de passe ne vivent que dans
le trousseau de macOS (ou dans `PATCHNAMES_EBIRD_USER`,
`PATCHNAMES_EBIRD_PASSWORD`, `PATCHNAMES_IMAP_USER`,
`PATCHNAMES_IMAP_PASSWORD`), jamais dans un fichier.

### Les doublons

Une photo archivée à la mauvaise heure, puis corrigée, laisse deux
fichiers : la même image, une autre heure dans son EXIF et dans son nom.

```bash
patchdoc doublons                 # dire ce qui serait fait, sans rien toucher
patchdoc doublons 2026_07 voyage  # seulement ce qui touche ces dossiers
patchdoc doublons --deplacer      # ranger les fichiers en trop dans doublons/
```

- **La même image.** Deux fichiers sont le même cliché quand leurs données
  image sont identiques, quoi que disent leurs métadonnées (l'empreinte
  `ImageDataHash` d'exiftool). Ne sont lus que les fichiers qui pourraient
  l'être : même extension, taille presque égale, et le même numéro
  d'appareil dans leur nom, ou le même nom. Les empreintes sont gardées
  dans la base : une recherche arrêtée (Ctrl-C) ou relancée ne relit que ce
  qu'elle n'a pas lu.
- **Lequel est le bon.** Vos listes eBird ont la bonne heure : l'heure qui
  tombe pendant une liste l'emporte. Quand les listes ne tranchent pas (pas
  de liste, ou les deux heures dans la même), les photos du même jour qui
  ont le même écart et que les listes départagent décident pour les
  autres ; à défaut, celles des mêmes dossiers. Parmi les fichiers à la
  bonne heure, celui qui est dans l'arborescence de l'archive
  (`20YY_MM/oiseaux` ou `autres`) est gardé. Un groupe que rien ne
  départage est laissé en place et listé.
- **Rien n'est effacé.** `--deplacer` range les fichiers en trop dans
  `doublons/`, à la racine de l'archive, sous le chemin qu'ils avaient ;
  vous les supprimez vous-même. Le détail, fichier par fichier, est écrit
  dans `doublons.tsv` à côté de la base, avec la raison de chaque choix.
- **Les images tirées d'un NEF.** Un JPG ou un DNG fait à partir d'un NEF
  avant que son heure soit corrigée est resté à l'ancienne. Le NEF a
  raison : l'image prend son heure (EXIF) et son nom. Le NEF est reconnu à
  son numéro d'appareil, à moins de deux jours, et au même appareil et à la
  même exposition.
- **Les deux copies de l'archive.** Ce qui a été rangé sur une copie est
  retenu dans la base : la copie d'un disque à l'autre ne ramène jamais un
  doublon, ne copie jamais le dossier `doublons/`, et range les mêmes
  fichiers de côté sur l'autre copie, dans son propre dossier `doublons/`.

### La page de recherche et sa base de données

`patchdoc gui` ouvre http://127.0.0.1:8765/ (`--port`), servie à cette
machine seulement. Chaque tuile est une séquence ; un clic montre la photo,
la description complète, les espèces, le lieu, la liste eBird avec ses
espèces, et le fichier (Afficher dans le Finder, Ouvrir). Filtres : texte
libre, espèce, lieu, région, année et dates, sujet, téléobjectif ou non, et
ce qui demande un coup d'oeil (identification à confirmer, espèce absente
de la liste, photos d'oiseaux sans liste).

- **Tri.** Au-dessus de la galerie : « Plus récentes », « Plus anciennes »,
  « Par espèce » (les séquences qui ont une espèce d'abord, dans l'ordre
  alphabétique), « Panoramas d'abord » et « Personnes d'abord » (les photos
  où quelqu'un est nommé, puis celles où un nom est proposé, puis les
  visages pas encore nommés). Une photo sans date, ou datée dans le futur
  par un appareil mal réglé, vient toujours à la fin.
- **Sections.** Tout, Oiseaux, Famille (les photos où il y a des
  personnes), Autre : chaque section montre ses filtres. Pour les oiseaux :
  l'espèce, la liste eBird, et les trois notes que Claude donne de 1 à 5,
  la netteté de la mise au point sur l'œil, à quel point l'oiseau est
  dégagé, et à quel point la photo est prise à la hauteur de son œil. Ces
  notes sont aussi des mots-clés (`œil net 4/5`) et une hiérarchie Lightroom
  (`Qualité|Œil net|4`). Dans une rafale, chaque vue a les siennes et les
  filtres portent sur la meilleure (voir « Les notes d'une photo
  d'oiseau »).
- **Carte des lieux.** Un disque par lieu, avec son nombre de séquences ;
  les disques se regroupent quand on dézoome et se séparent quand on zoome.
  Un clic sur un regroupement zoome dessus, un clic sur un lieu le choisit
  comme filtre. La carte demande Internet (OpenStreetMap).
- **Fichiers bruts.** « Ouvrir avec Affinity » ouvre le NEF affiché dans
  l'application de la config `raw_app`.

### Les lieux des photos de cellulaire

Une photo qui porte sa propre position la garde et reçoit son lieu de cette
position, qu'elle ait été prise pendant une liste eBird ou non : le
quartier, la ville, la province et le pays sont demandés à Nominatim, le
géocodeur d'OpenStreetMap, ou viennent de votre propre lieu eBird s'il y en
a un à moins de 300 m. Le lieu et la position d'une liste eBird ne servent
qu'aux photos qui n'ont pas de position. Une photo de cellulaire prise
pendant une liste reste liée à cette liste (ses espèces comme a priori, la
liste comme événement), mais ni son lieu ni sa latitude et sa longitude
n'en viennent ; si rien ne nomme sa position (en pleine nature, sans
réseau), elle prend le nom du lieu de la liste et garde sa position.

Seule la position part en ligne, arrondie à environ 100 m ; chaque réponse
est gardée dans la base. `"geocode": false` dans la config pour ne jamais
rien demander. La position d'une photo n'est connue de la base que si
`patchdoc` a traité la photo ou si `patchdoc index --exif` a lu son
dossier ; « Écrire dans les fichiers » la découvre aussi, et corrige alors
le lieu dans la base. Pour les photos de cellulaire déjà indexées avec le
lieu de leur liste : `patchdoc index --exif DOSSIER --force`, puis « Écrire
dans les fichiers ».

### Les personnes

Claude ne reconnaît personne à son visage, avec ou sans photos de référence,
et `patchdoc` ne le lui demande pas. Les visages sont trouvés et comparés
sur votre machine, par deux petits modèles d'OpenCV téléchargés une fois
(`pip install opencv-python`) ; rien ne part en ligne.

- Dans la visionneuse, « Montrer les visages » met un carré sur chaque
  visage de la vue affichée. Vous donnez un nom à un visage, avec si vous
  le voulez l'année de naissance de la personne. Le nom se choisit dans
  une liste : d'abord ceux de la config `people`
  (`{"people": ["Alice", "Bob"]}` dans votre `config.json`), puis ceux
  déjà donnés, puis « + ajouter un nom… » pour en taper un nouveau.
- Ce visage devient un exemple, daté par sa photo. Les autres visages lui
  sont comparés et le nom est proposé, avec un point d'interrogation, là où
  la ressemblance est forte ; « Oui » le confirme, « Non » l'écarte pour de
  bon.
- **L'âge.** Un exemple ne vaut que pour les années autour de sa photo :
  un an pour un bébé, deux ou trois pour un enfant, douze pour un adulte
  (quatre si l'année de naissance n'est pas donnée), et personne n'est
  cherché avant sa naissance. Un visage reconnu avec une forte ressemblance
  sert d'exemple à son tour : un nom donné à huit ans remonte ainsi, photo
  après photo, vers le bébé. Nommer la même personne à quelques âges
  différents est ce qui l'aide le plus.
- Les noms confirmés vont dans la base et dans les fichiers
  (`PersonInImage`, mot-clé, hiérarchie `Personne|Nom`), les noms proposés
  seulement dans la base. Ils sont donnés à Claude avec la photo, pour
  qu'il les emploie dans sa description.
- La page cherche les visages des photos qui ne sont pas d'oiseaux en
  arrière-plan, après les vignettes (`gui_scan_faces`).
- Les animaux de la famille se nomment dans `keywords.md` (« le chien blanc
  s'appelle ... ») : Claude les reconnaît à leur description.

La page n'interroge pas les disques mais une base de données locale :
`~/photo/patchdoc/photos.sqlite` (config `index_dir`), avec à côté un dépôt
local de vignettes (`thumbs/`, 360 px) et d'aperçus (`previews/`, 1600 px).
La recherche et la navigation marchent donc sans le disque d'archive ; seuls
l'ouverture d'un fichier, la fabrication d'une vignette qui manque et
l'appel à Claude le demandent.

- **Le dépôt de vignettes se remplit tout seul.** Tant que la page est
  servie et que le disque est monté, les vignettes qui manquent sont faites
  en arrière-plan, celle de chaque séquence, les plus récentes d'abord ; la
  page dit où il en est (`patchdoc gui --no-vignettes` ou la config
  `gui_fill_thumbs` pour s'en passer). `patchdoc vignettes` fait la même
  chose sans la page. Les vignettes attendent pendant que Claude identifie
  des séquences, pour lui laisser le disque.
- **« Identifier la séquence avec Claude ».** Dans la visionneuse, ce bouton
  montre la séquence à Claude là où sont ses fichiers (la vue du milieu,
  l'agrandissement au point de mise au point, une planche de douze vues au
  plus) avec sa liste eBird. La réponse arrive en une vingtaine de secondes
  dans la page et dans la base, puis elle est écrite dans l'en-tête de
  chaque fichier de la séquence, avec le lieu et la position de la liste.
  Pour une rafale d'oiseau, chaque vue est d'abord lue sur le disque
  d'archive pour être notée (la page dit où elle en est), quatre vues à la
  fois : 1,6 à 3,8 s par vue mesurées sur un disque réseau occupé, là où
  une lecture vue par vue en prenait environ 7 ; moins d'une seconde par
  vue quand exiftool tourne sur le serveur (voir plus bas).
  Sur un disque réseau, cette écriture récrit chaque fichier et peut prendre
  des minutes pour une longue rafale ; la page dit où elle en est, et une
  écriture coupée par l'arrêt du serveur reprend à son prochain démarrage.
  Les fichiers sont récrits sur place, sans copie `<fichier>_original`
  (config `gui_backup` pour en garder une).
- **Tout ce qui est affiché, en arrière-plan.** Au-dessus des résultats, un
  bouton fait la même chose pour toutes les séquences affichées qui n'ont
  pas de description, deux à la fois ; un premier clic dit combien d'appels
  à Claude le second lancera. Le travail continue si la page est fermée, et
  la page peut l'arrêter. Elle dit où il en est comme une barre de
  progression de terminal : la part faite (comptée en photos), le temps
  écoulé, le temps restant à ce rythme, et pour chaque séquence en cours ce
  qui se passe (« lecture des vues 7 sur 11 », « Claude regarde la
  séquence »). Les réponses arrivent dans la page et dans la base au rythme
  de Claude ; l'écriture dans les fichiers, que le disque réseau rend bien
  plus lente, suit à part, une séquence après l'autre, avec sa propre ligne
  (« fichier 6 sur 11 »). Elle reprend toute seule si la page est relancée.
- **Toute l'archive, en arrière-plan.** À côté, « Identifier toute
  l'archive » met en file toutes les séquences de l'archive qui n'ont pas de
  description, les plus récentes d'abord, quoi que la page affiche (les
  photos pas encore archivées n'en font pas partie). Un premier clic dit
  combien de séquences le second lancera. Pour une grande archive, c'est un
  travail de plusieurs jours : il continue si la page est fermée, et il
  reprend de lui-même quand `patchdoc gui` est relancé, jusqu'à ce que la
  file soit vide ou que « Arrêter » y mette fin. Les séquences demandées
  entre-temps avec le bouton voisin passent devant. Si quatre séquences de
  suite échouent (limite d'utilisation de Claude atteinte, réseau coupé),
  la file attend 10 minutes, puis 20, 40 et 60 au plus, et réessaie ; une
  séquence en échec est reprise une fois, après que Claude a répondu de
  nouveau. Si le disque d'archive n'est plus monté, la file attend son
  retour.
- **Les photos pas encore archivées.** En haut de la page, « Regarder »
  choisit ce qui est montré : tout, l'archive, ou les photos qui attendent
  dans `~/photo` avant d'être envoyées à l'archive (config `inbox_dirs` :
  par défaut `~/photo`, `~/photo/oiseaux` et `~/photo/autres`, les fichiers
  posés directement dans chacun, pas leurs autres sous-dossiers). Choisir
  ces photos relit les dossiers et ajoute à la base ce qui est nouveau,
  sans rien renommer ni déplacer. Elles s'identifient, se notent et se
  corrigent alors comme celles de l'archive, et ce qui est dit est écrit
  dans leurs fichiers là où ils sont. Ensuite `patchdoc`, lancé dans leur
  dossier, les renomme et les archive avec ce qui a été noté, sans rappeler
  Claude ; la base suit. Une photo dont la base a déjà le nom ailleurs est
  laissée de côté.
- **La loupe sur l'œil.** Une photo d'oiseau dont l'œil a été trouvé porte
  une petite icône de loupe : sur sa tuile, sur la photo de la visionneuse
  et sur chaque vue de la bande. Le curseur sur cette icône (ou un clic)
  montre un carré de 512 pixels autour de l'œil, à pleine résolution, avec
  sa note ; dans la visionneuse, un anneau marque alors l'œil sur la
  photo. Rien ne s'affiche ailleurs qu'en passant sur l'icône. S'il y a plusieurs oiseaux, c'est l'œil
  du plus net. La position de l'œil est gardée dans la base (`eye_x`,
  `eye_y`, en fractions de la photo) et écrite dans le fichier comme une
  région XMP nommée « œil », de type Focus, dans le schéma des régions du
  Metadata Working Group (celui des visages dans les logiciels photo) :
  elle voyage avec le fichier, et `patchdoc index --exif` la relit.
  `{"doc_eye_region": false}` ne l'écrit pas dans les fichiers. Les carrés
  sont gardés à côté des vignettes ; une séquence décrite avant cette
  version n'en a pas tant qu'elle n'est pas identifiée de nouveau.
- **Corriger une identification.** Dans la visionneuse, sous « Espèces »,
  chaque espèce d'une séquence a un choix : une autre espèce de sa liste
  eBird, « Indéterminé », ou « + autre espèce… » pour en taper une (son nom
  latin vient de vos données eBird). « Confirmer » dit qu'une identification
  incertaine était juste. Dans tous les cas c'est votre parole : la
  séquence devient « identification vérifiée » (un mot-clé dans les
  fichiers aussi), le titre est refait, « sur la liste » est revu, et les
  fichiers sont récrits. Une description écrite pour une autre espèce est
  remplacée par une ligne neutre ; « Identifier de nouveau avec Claude » la
  refait alors en tenant votre espèce pour acquise, sans la remettre en
  cause.
- **« Écrire dans les fichiers ».** Le bouton voisin fait seulement la
  seconde étape, sans appeler Claude : il écrit dans les fichiers ce que la
  base sait déjà de la séquence. Pour une séquence jamais décrite mais prise
  pendant une liste eBird, c'est le lieu, la liste et la position.

- **Des photos sur des disques externes ou réseau.** Le chemin d'une photo
  archivée est gardé relatif à l'archive. Si l'archive n'est pas toujours
  montée au même endroit (le disque branché directement, un autre montage
  réseau), donnez les autres endroits dans la config : le premier qui est là
  est utilisé, pour chercher comme pour archiver.

  ```json
  {"photo_archive_root": "~/mnt/URUBU/photo", "photo_archive_mounts": ["/Volumes/URUBU/photo"]}
  ```

- **Deux copies de l'archive.** L'archive peut vivre sur deux disques qui
  sont la copie l'un de l'autre, par exemple un disque réseau et un disque
  externe : `photo_archive_root` et `photo_archive_mounts`.

  ```json
  {"photo_archive_root": "~/mnt/URUBU/photo", "photo_archive_mounts": ["/Volumes/u5tb/photo"]}
  ```

  Sous le titre, la page montre les copies et celle qu'elle utilise (la
  première qui est là ; un clic sur une autre copie branchée la choisit, et
  `patchnames` et `patchdoc` y archivent alors aussi). Les boutons
  « A → B » et « B → A » portent vers une copie ce que l'autre a de plus
  récent ou qu'elle n'a pas, avec rsync : la page compare d'abord et dit
  combien de fichiers et quel volume, vous confirmez, puis une barre suit
  la copie (fichiers, volume, débit, temps restant) et un bouton l'arrête.
  Un fichier n'est remplacé que par un plus récent, et rien n'est jamais
  effacé : faire les deux sens laisse à chaque copie le plus récent de
  tout. Une copie montée par sshfs est jointe par rsync directement sur son
  serveur (`rsync -rtuz -e ssh`), ce qui est bien plus rapide qu'à travers
  le montage ; `photo_archive_remotes`
  (`{"~/mnt/URUBU/photo": "moi@serveur:/chemin/photo"}`) le dit pour une
  copie qui n'est pas montée. Une limite : un fichier renommé sur une copie
  (un fichier sans numéro d'appareil change de nom quand ses étiquettes
  sont écrites) reste sous son ancien nom sur l'autre.

  La base de recherche, elle, n'existe que sur votre machine. À la fin de
  chaque copie d'un disque à l'autre (et quand la comparaison ne trouve
  rien à copier), une copie en est laissée sur les deux disques :
  `patchdoc/photos.sqlite` à la racine de l'archive, la précédente gardée
  sous le nom `photos.avant.sqlite`. Elle est prise par la sauvegarde de
  SQLite, sûre pendant que la page tourne, et n'est refaite que si la base
  a changé. La page ne la lit jamais : pour restaurer, recopiez le fichier
  dans `~/photo/patchdoc/`. Les vignettes n'en font pas partie, elles se
  refont. Ordre de grandeur : 120 Mo pour 240 000 photos dont peu sont
  décrites, environ 300 Mo si toutes l'étaient.
- **exiftool sur le serveur de l'archive.** Une archive montée par sshfs
  répond lentement : chaque fichier qu'exiftool ouvre est un aller-retour,
  et écrire une étiquette fait venir le fichier entier puis le renvoie. Si
  le serveur a exiftool (`sudo apt install libimage-exiftool-perl`),
  `patchdoc` le lance là-bas par ssh : le serveur lit et récrit son propre
  disque, et seul ce qui est demandé voyage (les étiquettes, l'image
  contenue dans un fichier brut). Rien à régler : la page dit sous le titre
  si c'est le cas, et tout se fait ici comme avant quand le serveur n'a pas
  exiftool ou ne répond pas. `{"remote_exiftool": false}` l'interdit, ou
  donne le chemin d'exiftool sur le serveur s'il n'est pas dans le PATH.
  Mesuré avec un Raspberry Pi pour serveur, par fichier NEF : écrire ses
  étiquettes, 0,4 s au lieu de 13 ; lire ses étiquettes, 0,2 s au lieu de
  0,8 ; ses étiquettes et l'image qu'il contient, 0,8 s au lieu de 2,5.
- **SQLite plutôt que DuckDB.** La page lit la base pendant que `patchdoc` y
  écrit, ce qu'un fichier DuckDB n'accepte pas entre deux processus, et sa
  recherche plein texte suit chaque ajout. Les colonnes sont typées : DuckDB
  lit le fichier tel quel pour vos propres analyses. La base tient son
  journal en mode « write-ahead » : une lecture (la page, DuckDB, un script
  à vous) ne fait jamais attendre une écriture, ni une écriture une
  lecture ; `photos.sqlite-wal` et `photos.sqlite-shm` vivent donc à côté
  de `photos.sqlite`.

  ```python
  import os
  import duckdb
  base = os.path.expanduser('~/photo/patchdoc/photos.sqlite')
  con = duckdb.connect()
  con.execute(f"ATTACH '{base}' AS idx (TYPE sqlite, READ_ONLY)")
  con.sql("SELECT substr(taken, 1, 4) AS annee, count(*) AS photos, "
          "count(DISTINCT checklist) AS listes FROM idx.photos GROUP BY 1 ORDER BY 1").show()
  ```

  Tables : `photos` (une ligne par fichier ; `rep = 1` pour la vue qui
  représente sa séquence), `tags`, `checklists` et `observations` (vos
  données eBird).

Ce qui remplit la base avec ce qui est déjà archivé, sans jamais appeler
Claude :

```bash
patchdoc index                                    # toute l'archive, d'après les noms des fichiers
patchdoc index --exif ~/mnt/URUBU/photo/2026_10   # lire ce que les fichiers d'un dossier disent d'eux-mêmes
patchdoc index --rematch                          # refaire l'accord avec les listes eBird (après un nouvel export)
patchdoc vignettes --year 2026                    # faire d'avance les vignettes (disque monté)
```

La première prend quelques minutes : le nom d'une photo donne sa date et son
heure, son dossier dit si elle a été prise au téléobjectif, et les listes
eBird donnent le lieu. Ces photos ne sont pas décrites, mais on les trouve
par lieu et par date, et par les espèces de leur liste (la case sous les
filtres). La deuxième lit le titre, la description et les mots-clés déjà
écrits dans les fichiers (par `patchdoc` ou `patchnames keywords`), ainsi
que l'appareil et l'objectif ; comptez une ou deux secondes par photo sur un
disque réseau. Elle travaille jour par jour et saute ce qu'elle a déjà lu :
on peut l'arrêter et la relancer. La troisième refait, d'après la base
seule et en une seconde, l'accord de chaque séquence avec les listes eBird :
utile quand une liste a été saisie après l'indexation de ses photos. La
quatrième fait les vignettes des vues
qui représentent les séquences (`--all` pour toutes les vues, `--previews`
pour l'image de la visionneuse), les plus récentes d'abord ; sans elle, une
vignette est faite la première fois qu'elle est affichée, lentement si le
disque est en réseau.

## Configuration

`~/.config/patchnames/config.json`:

```json
{
  "exif_cache_dir": "~/photo/exifdata",
  "photo_archive_root": "~/mnt/URUBU/photo",
  "dng_converter": "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter",
  "extensions": ["*.NEF", "*.JPG", "*.HEIC", "*.MP4", "*.DNG", "*.MOV", "*.DCM", "*.PSD"],
  "camera_owners": {
    "EA": ["D90", "NIKON Z 50", "NIKON Z50_2"]
  },
  "bird_lenses": ["200-500mm", "300mm"],
  "photo_archive_mounts": [],
  "inbox_dirs": ["~/photo", "~/photo/oiseaux", "~/photo/autres"],
  "photo_archive_remotes": {},
  "remote_exiftool": null,
  "bird_focal_min": 200,
  "doc_budget": 0,
  "people": [],
  "index_dir": "~/photo/patchdoc",
  "ebird_dir": "~/.config/patchnames/ebird",
  "ebird_search": ["~/Downloads/ebird_*.zip", "~/Downloads/MyEBirdData*.csv"]
}
```

`PATCHNAMES_CONFIG=/path/to/other.json` uses another config file, to try
things away from the archive.

### Archive layout

When `photo_archive_root` is mounted, files are moved to:

```
photo_archive_root/
  20YY_MM/
    oiseaux/   # shots with a telephoto lens (≥300 mm)
    autres/    # everything else
```

## Python API

```python
from patchnames import patchnames, time_offset, patchdate, nef_to_dng
```
