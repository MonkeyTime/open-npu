# OPEN-NPU

*Open Participatory Edge Network for Neural Processing Units*

Pour préparer un déploiement fermé, voir [le guide de préproduction](deploy/PILOTE.md) et [le contrat OpenAPI des calculs](docs/openapi.json).

Prototype local de calcul participatif. Le portail recueille les projets. Le super administrateur les approuve et fournit une clé API par projet. Les agents Windows s’inscrivent automatiquement, interrogent le serveur et exécutent les lots sur Intel AI Boost avec OpenVINO.

## Utiliser l’instance locale

- Portail `http://127.0.0.1:8787/`
- Administration `http://127.0.0.1:8787/admin`, identifiant `superadmin`. Le mot de passe se trouve dans `runtime/superadmin.txt`. Le répertoire `runtime/` contient aussi la base SQLite et les jetons locaux. Gardez-le privé.
- Agent Windows `dist/OpenNpuAgent/OpenNpuAgent.exe`. Conservez le répertoire `_internal` à côté de l’exécutable.
- Journal de l’agent `runtime/agent.log`.

Les scripts suivants lancent le serveur et un agent de développement sur le même poste. Au premier lancement, l’agent local s’inscrit automatiquement.

```powershell
.\start-local.ps1
.\start-agent.ps1
```

Chaque script affiche son PID. Arrêtez le processus voulu avec `Stop-Process -Id <PID>` ou depuis le Gestionnaire des tâches. La désactivation d’un nœud dans l’administration libère les lots qu’il détenait et suspend l’agent. Si l’agent tourne toujours, il reprend automatiquement après réactivation. Les scripts démarrent en arrière-plan pour la session courante.

### Installer l’agent sur un poste Windows

Préparez une seule fois un dossier d’installation pour le réseau de participants. Après avoir construit l’agent et démarré le serveur, exécutez `./new-agent-package.ps1 -Server <URL>`. Utilisez l’adresse HTTPS que les postes peuvent joindre. Pour un essai sur la machine du serveur uniquement, utilisez `http://127.0.0.1:8787`.

Sur chaque poste, exécutez `./install-agent.ps1` depuis le dossier distribué. L’installation se fait dans le profil Windows courant, démarre l’agent en arrière-plan et le relance à l’ouverture de session. Le nom de l’ordinateur apparaît dans la flotte dès l’inscription. Les diagnostics sont dans `%LOCALAPPDATA%\OPEN-NPU\agent.log`. L’option `-NoStartup` permet un essai sans lancement à l’ouverture de session.

L’icône OPEN-NPU apparaît dans la zone de notification Windows, près de l’horloge ou sous la flèche des icônes masquées. Survolez-la pour lire l’état de l’agent. Un clic droit permet d’activer ou de désactiver le partage, de régler la limite d’envoi, d’ouvrir le journal ou de quitter l’agent. La pause laisse le lot en cours se terminer et conserve les réservations, mais l’agent reste exclu des nouvelles attributions et réservations jusqu’à la reprise.

Le dossier distribué contient une clé d’inscription commune. Elle permet d’inscrire un poste. Chaque agent génère ensuite une identité distincte qu’il conserve après une réinstallation. Réservez le dossier aux participants et conservez le fichier `node.json` dans le profil local.

Les paramètres de contribution permettent aussi d’exiger une alimentation secteur et de limiter le partage à une plage quotidienne, à l’heure locale de Windows. Une plage peut traverser minuit. Un lot déjà commencé peut se terminer.

### Gérer la flotte

L’administration contient des rubriques dédiées **Calculs**, **Accès** et **Supervision**. Les comptes nominatifs peuvent utiliser un code TOTP et reçoivent un rôle propriétaire, opérateur ou lecture seule. Le compte technique `superadmin` reste disponible avec HTTP Basic pour l’amorçage. Un propriétaire peut révoquer définitivement l’identité d’un nœud. Une nouvelle inscription est alors nécessaire pour ce poste.

La rubrique **Accès** gère les invitations de postes et les clés API nommées à durée définie, en lecture seule ou en lecture et écriture. Une clé en lecture seule ne peut ni soumettre ni annuler. La rubrique **Calculs** filtre et pagine les demandes, permet l’annulation et relance une demande annulée ou en échec sous un nouvel identifiant. La fiche d’un projet approuvé permet de suspendre l’admission et de définir ses quotas ainsi que sa priorité.

Dans l’administration, recherchez un nœud par nom, identifiant ou accélérateur. Les compteurs filtrent la liste par état et le tableau affiche de 25 à 100 nœuds par page. Une sélection est conservée pendant la navigation et l’actualisation. Vous pouvez désactiver ou réactiver jusqu’à 100 nœuds en une action après avoir vérifié la sélection.

Le tableau indique le dernier contact et distingue les nœuds en calcul, disponibles, hors ligne, jamais connectés ou désactivés. L’historique affiche les dernières actions et le nombre de lots remis en file. Le tableau se rafraîchit automatiquement lorsque l’onglet est visible. En cas de problème de connexion, il conserve les dernières données et indique l’échec.

Le journal d’administration `/admin/history` permet de filtrer par période, origine, type d’action, projet et nœud. Il enregistre les inscriptions, activations et désactivations, décisions et modifications de projet, remplacements de clé et changements de réservation. Il indique les entités concernées et, pour une désactivation, le nombre de lots remis en file. Les clés, jetons et contenus de calcul restent hors du journal. Les anciennes actions de flotte disponibles sont reprises lors de la migration.

La liste des projets se trouve sur `/admin/projects`. Elle permet de chercher un projet par nom, établissement, contact ou identifiant, de filtrer par statut ou réservation et de paginer les résultats. Ouvrez une fiche pour modifier les informations, instruire le projet, gérer sa clé et examiner les demandes de calcul récentes. Le remplacement d’une clé API invalide l’ancienne immédiatement. La nouvelle clé permet de consulter les demandes déjà soumises.

Pour un projet approuvé, saisissez le nombre de nœuds à réserver. La sélection automatique privilégie un groupe ayant le même profil matériel déclaré par OpenVINO (nom, architecture et nombre maximal de tuiles). Elle peut compléter le groupe avec d’autres profils, ou avec des agents plus anciens qui ne déclarent que le nom de l’accélérateur. Pour imposer un groupe précis, cochez ses nœuds dans la flotte, cliquez sur **Affecter à un projet**, ouvrez la fiche voulue puis choisissez **Réserver la sélection**. Si l’effectif exact est indisponible, la réservation existante est conservée.

Un nœud réservé prend uniquement les nouveaux lots du projet auquel il est attribué. Les nœuds du pool partagé traitent les projets sans réservation. Une réservation reste en place si un de ses nœuds se déconnecte ou est désactivé. Lorsqu’on libère une réservation, les lots déjà attribués restent sous leur bail et les prochains lots redeviennent accessibles au pool partagé.

## Reconstruire

Prérequis de compilation Windows x64, Python 3.12 et un pilote Intel NPU fonctionnel pour l’exécution.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\build-agent.ps1
```

Si vous utilisez le `uv.exe` local pour gérer Python, remplacez les deux premières commandes par `.\.tools\uv.exe venv --python 3.12` puis `.\.tools\uv.exe pip install -r requirements.txt`.

Pour vérifier l’accélérateur du poste indépendamment du serveur, lancez l’exécutable avec `--probe` et consultez `runtime/agent.log`. L’inférence cible explicitement `NPU` dans OpenVINO.

## Envoyer un calcul

Créez un projet sur le portail et approuvez-le dans l’administration. La clé API s’affiche à l’approbation. Elle donne accès aux demandes et aux résultats de ce projet.

```powershell
$headers = @{ Authorization = "Bearer $env:OPEN_NPU_API_KEY" }
$body = @{ capsule = 'square-fp16-v1'; values = @(2, -3, 4) } | ConvertTo-Json -Compress
$job = Invoke-RestMethod 'http://127.0.0.1:8787/v1/jobs' -Method Post -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod "http://127.0.0.1:8787/v1/jobs/$($job.id)" -Headers $headers
```

La seconde requête peut retourner `queued`, `running`, `failed`, `cancelled` ou `done`. En cas de succès, `values` contient les résultats dans l’ordre d’entrée. La capsule de démonstration accepte de 1 à 4096 nombres compris entre −200 et 200. Elle élève chaque valeur au carré en FP16, avec les arrondis de ce format.

## Répartition des calculs

La file découpe chaque demande en lots de 16 valeurs au maximum. Chaque agent compatible prend un lot à la fois. Si un agent disparaît, son lot redevient disponible à l’expiration du bail de 120 secondes, jusqu’à trois tentatives. Quand un lot échoue définitivement, les autres lots en attente ou en cours de la même demande sont annulés. Le contrat de la capsule fixe la forme, la précision et l’opération exécutée. Le graphe est intégré au binaire. Les demandes contiennent uniquement l’identifiant de la capsule et les données d’entrée.

### Débit et attribution des lots

L’agent mesure le temps de réponse HTTP du serveur et estime les débits montant et descendant avec une sonde de 64 Kio par sens, au plus toutes les 30 minutes. En boucle locale, il remonte uniquement le temps de réponse. Pour les transferts trop rapides pour cette sonde, le débit reste non renseigné. La flotte indique l’âge de la mesure. Le serveur ignore les estimations réseau après deux heures.

Dans **Paramètres** de l’agent, **Limiter les envois** fixe le plafond en Mbit/s pour les résultats et la sonde. Le changement s’applique aux prochains envois sans redémarrage. Les préférences de partage et de débit sont conservées à la relance. Pour une installation automatisée, `./install-agent.ps1 -UploadLimitMbps 2` fixe un plafond de 2 Mbit/s et `-UploadLimitMbps 0` le retire. Le débit observé tient compte du plafond lorsque la sonde est limitée. Les petits messages de contrôle ne sont pas cadencés.

À la soumission, le serveur peut réduire la taille des nouveaux lots de la capsule selon le budget estimé des nœuds éligibles. À chaque attribution, il vérifie que l’estimation de transfert et la marge de calcul restent compatibles avec le bail. Les mesures absentes ou expirées n’empêchent pas un agent de traiter les petits lots. Le plafond déclaré reste utilisé.

Ce modèle permet d’exécuter davantage de lots en parallèle. Chaque lot doit tenir sur une NPU. Pour des calculs avec plusieurs étapes dépendantes, il faudrait étendre l’ordonnanceur pour transmettre les résultats intermédiaires. Pour distribuer un modèle trop gros pour un seul appareil, son découpage et le coût des échanges devront être évalués pour ce modèle.

L’instance actuelle écoute sur la boucle locale. Un réseau de volontaires nécessitera un point d’accès HTTPS et un mécanisme de vérification des résultats fournis par les nœuds. Le test local valide le matériel et le parcours API pour une capsule de démonstration.
