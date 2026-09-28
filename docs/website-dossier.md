# Dossier source d’un site web — adaptateur HTML initial

Cette tranche fournit `capture_website()` et un contrat de dossier sourcé. Elle
n’ajoute ni compétence déclarée, ni API publique, ni worker, ni interface de
création. Aucun site client n’a été parcouru : aucune URL client n’a été fournie.
Le raccordement au projet, la persistance versionnée et l’affichage restent une
étape distincte. Le moteur de recherche existant n’est pas remplacé.

## Contenu conservé

Le collecteur consulte l’URL publique, `robots.txt`, jusqu’à trois sitemaps et
les liens HTML internes dans la limite choisie. Chaque page extraite conserve
son URL demandée/finale, date, statut HTTP, empreinte SHA256 du corps reçu,
titre, langue, texte du HTML, titres de section, métadonnées, JSON-LD parsable,
liens, références d’images/documents et structure des formulaires. Les valeurs
des attributs de saisie ne sont pas copiées et aucun formulaire n’est soumis.

L’inventaire contient les textes et les références nécessaires à une future
migration : offres, coordonnées, horaires, prix, FAQ ou témoignages présents
peuvent être repris depuis les passages sources. Le collecteur ne déduit pas
ces catégories ni ne transforme leurs affirmations en faits vérifiés. Chaque
élément porte l’URL source, l’empreinte de sa page et un repère d’extraction.
Ces repères identifient des balises ou positions dans cette capture HTML ; ce
ne sont pas des sélecteurs de navigateur universels.

La provenance est `source_site_statement` et le dossier entier reste du contenu
externe non vérifié. Les textes du site sont des données, jamais des consignes
pour l’agent ni des autorisations d’action. Aucun modèle n’est appelé.

## Migration vers le futur site

Chaque entrée commence avec `disposition=unassigned` et
`destination_url=null`. Le contrat réserve les états `retained` (repris),
`grouped` (regroupé), `rewritten` (reformulé), `needs_confirmation`
(à confirmer) et `excluded` (écarté avec un motif obligatoire). Aucun état de
migration, destination, choix de marque ou contenu commercial n’est inventé
par le collecteur. Un inventaire non affecté ne peut pas porter une destination.

`migration_audit=not_run` indique qu’aucun site reconstruit n’a encore été
comparé à l’inventaire. Le futur constructeur devra relier les sources aux
pages produites et exécuter cet audit d’omissions ; cette tranche ne le fait pas.

## Couverture et limites

Par défaut : 12 pages tentées, 200 URL découvertes, 256 Kio par réponse,
2 Mio de corps reçus, 60 secondes au total, trois redirections par ressource,
trois sitemaps et 32 000 caractères de texte par page. Les bornes maximales
acceptées sont inscrites dans `CaptureLimits`. Un octet supplémentaire peut
être lu pour détecter le dépassement d’un plafond ; il est comptabilisé et
aucune autre ressource n’est lue une fois le budget total atteint.

La couverture distingue les pages extraites, échecs, blocages, exclusions et
URL découvertes non visitées. Les erreurs de découverte par sitemap et la
saturation de l’inventaire restent visibles. Les champs textuels tronqués
sont signalés. `bounded_scope_exhausted` signifie seulement que le parcours
HTML découvert dans ce périmètre est épuisé ; ce n’est pas une certification
de connaissance de toutes les pages du site. Tout blocage, échec, texte tronqué
ou limite de découverte donne `partial`.

Les destinations restent sur le même nom d’hôte et les ports HTTP/HTTPS
standards. Une montée HTTP vers HTTPS est permise, pas le retour vers HTTP.
Ce contrôle s'applique à chaque saut de redirection, même si l'URL initiale
était en HTTP. Un lien HTTP découvert dans une page HTTPS reste dans
l'inventaire source, mais son téléchargement est exclu avec `tls_downgrade`
et la couverture est marquée partielle.
Les redirections vers un autre hôte, y compris un autre sous-domaine, sont
signalées au lieu de modifier le périmètre implicitement. Les fragments sont
retirés ; l’ordre des paramètres de requête est conservé.

Le transport utilise exclusivement GET, sans proxy d’environnement, cookies,
jeton ni authentification. Tous les résultats DNS doivent être publics ; la
connexion utilise l’adresse vérifiée sans seconde résolution, avec validation
TLS et SNI pour HTTPS. L’accès robots est vérifié avant chaque page et chaque
redirection de page. Un robots indisponible bloque le parcours ; un 404 indique
son absence. Cette version ne met pas encore en œuvre `Crawl-delay`, une reprise
incrémentale ou un stockage de cache partagé. Les réponses compressées sont
refusées et les tailles/délais restent bornés.

## Ce qui reste indisponible

- Rendu JavaScript, captures mobile/desktop, observation de parcours interactifs.
- Téléchargement, validation et stockage binaire des médias/documents. Une URL
  d’image ou un lien `.pdf` est une référence source, pas un actif téléchargé.
- Extraction des fichiers PDF, des images `srcset` ou des images embarquées.
- Vérification des affirmations commerciales, diagnostic marketing, branding
  et intégration du service Infographic Artist.
- Reconstruction, preview multipage, déploiement et audit de migration.

Le dossier expose explicitement `rendering=not_available` et
`asset_downloads=not_performed`. Le HTML peut contenir des éléments cachés ou
manquer du contenu ajouté par JavaScript ; son texte n’est pas présenté comme
une observation visuelle du navigateur.

## Validation locale

Depuis `server` :

```sh
.venv/bin/python -m pytest tests/test_website_dossier.py -q
.venv/bin/python -m mypy app/services/website_dossier.py app/services/website_dossier_contracts.py
.venv/bin/python -m ruff check app/services/website_dossier.py app/services/website_dossier_contracts.py tests/test_website_dossier.py
```

Les tests couvrent une fixture HTTP réelle et bénigne sur loopback, injectée
uniquement dans le test ; les règles réseau publiques de production restent
actives. Ils vérifient aussi les URL privées, les DNS mixtes, l’épinglage réseau,
les redirections, robots, les limites, les sitemaps, les provenances et les états
de migration. Aucun test ne publie, ne contacte un site client ou ne lance un
modèle. Une qualification réseau publique et TLS avec une URL autorisée reste
nécessaire avant l’activation d’un worker.
