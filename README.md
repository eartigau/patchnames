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
  garde.
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

- `patchdoc` lit chaque vue en pleine résolution et mesure sa netteté là où
  l'appareil a fait le point.
- Claude voit, dans le même appel, toutes les vues recadrées autour de leur
  point de mise au point (16 par planche, 96 vues au plus ; au-delà, une vue
  prend les notes de sa voisine), et les douze vues les plus nettes en
  pleine résolution, un carré de 512 pixels autour du point de mise au
  point. Il dit pour chaque vue si l'œil se voit, sa netteté, le dégagement
  et la hauteur d'œil.
- L'œil est jugé en pleine résolution quand il est près du point de mise au
  point ; sinon sur la vue réduite, plus grossièrement, et sa note ne
  dépasse alors pas 4. Une vue où l'œil ne se voit pas n'a pas de note
  d'œil ; une vue que l'oiseau a quittée n'en a aucune.
- La meilleure vue est celle dont le total est le plus haut, l'œil comptant
  double. Elle porte le mot-clé `meilleure vue` (`Qualité|Meilleure vue`)
  et c'est elle qui représente la séquence dans la page : la tuile, les
  filtres par note et la visionneuse s'ouvrent sur elle.
- Dans la visionneuse, chaque vue de la bande porte ses trois notes (œil,
  dégagé, hauteur ; un point quand il n'y en a pas) et une étoile marque la
  meilleure ; les notes affichées suivent la vue à l'écran.

Ce qu'il faut en savoir :

- La netteté mesurée dépend de ce qui se trouve sous le point de mise au
  point (un plumage contrasté « mesure » plus net qu'une joue unie) : elle
  sert à choisir les vues montrées en grand et à départager deux vues à
  égalité, pas à noter.
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

Une rafale d'oiseau coûte davantage, parce que chaque vue est montrée :
mesuré avec le modèle par défaut, 0,22 $ pour une rafale de 11 vues et
0,17 $ pour une de 7 vues ; 0,005 $ avec `--model haiku --effort low`, moins
sûr quand il juge l'œil (il en a manqué un bien visible aux essais).

Chaque passage annonce ce qu'il compte dépenser, et s'arrête à `doc_budget`
(10 $ ; `--budget`, 0 pour aucun plafond) : les séquences restantes
attendent dans le dossier le passage suivant. Pour changer le réglage une
fois pour toutes, dans `~/.config/patchnames/config.json` :

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

### La page de recherche et sa base de données

`patchdoc gui` ouvre http://127.0.0.1:8765/ (`--port`), servie à cette
machine seulement. Chaque tuile est une séquence ; un clic montre la photo,
la description complète, les espèces, le lieu, la liste eBird avec ses
espèces, et le fichier (Afficher dans le Finder, Ouvrir). Filtres : texte
libre, espèce, lieu, région, année et dates, sujet, téléobjectif ou non, et
ce qui demande un coup d'oeil (identification à confirmer, espèce absente
de la liste, photos d'oiseaux sans liste).

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

Une photo qui porte sa propre position et n'a pas été prise pendant une
liste eBird reçoit son lieu de cette position : le quartier, la ville, la
province et le pays sont demandés à Nominatim, le géocodeur
d'OpenStreetMap, ou viennent de votre propre lieu eBird s'il y en a un à
moins de 300 m. Seule la position part en ligne, arrondie à environ 100 m ;
chaque réponse est gardée dans la base. `"geocode": false` dans la config
pour ne jamais rien demander. La position d'une photo n'est connue de la
base que si `patchdoc` a traité la photo ou si `patchdoc index --exif` a lu
son dossier.

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
  une lecture vue par vue en prenait environ 7.
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
  "bird_focal_min": 200,
  "doc_budget": 10,
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
