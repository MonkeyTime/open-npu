# Pilote interuniversitaire OPEN-NPU

## Préparer la préproduction

Le mode `pilot` conserve la capsule de démonstration Intel AI Boost. Au démarrage il exige un nom d’hôte autorisé et un secret administrateur externe. Il désactive l’inscription avec la clé locale partagée. Les services exposés doivent passer par le frontal HTTPS. L’administration doit rester accessible uniquement via le réseau d’administration. Utilisez `superadmin` pour amorcer les comptes nominatifs depuis **Accès**, puis connectez ces opérateurs sur `/login`. Activez TOTP à la création et transmettez le mot de passe et le secret TOTP par des canaux sûrs. Les sauvegardes de la base contiennent les secrets TOTP et doivent rester chiffrées et privées.

1. Choisir le domaine, le réseau VPN d’administration et une machine Linux dédiée. Configurer le DNS, les accès entrants HTTPS et la persistance du stockage.
2. Copier `deploy/pilot.env.example` vers `deploy/.env` et renseigner le domaine, le CIDR réellement vu par Caddy et le chemin du secret. Inscrire dans ce fichier de secret au moins 32 caractères aléatoires. Sur Linux, rendre le fichier lisible pour le groupe du compte de service 10001. Vérifier le montage et ses droits avec la version de Compose installée.
3. Depuis `deploy`, exécuter `docker compose config`, puis `docker compose up --build -d` en préproduction. Vérifier le certificat, les deux routes de santé, l’accès administratif via VPN et son refus depuis un réseau externe.
4. Créer une invitation via `POST /admin/enrollment-invites`, avec par exemple `{"label":"Campus A","hours":24,"max_uses":5}`. Conserver la réponse dans un fichier privé et préparer le dossier Windows avec `./new-agent-package.ps1 -Server https://… -InvitationFile <fichier>`. Distribuer le paquet aux postes retenus. Les invitations sont listables et révocables via l’API. Leur libellé facilite le suivi et ne constitue pas une isolation par établissement.
5. Approuver un projet de test, soumettre une demande avec un identifiant unique dans `Idempotency-Key` et vérifier le résultat sur plusieurs postes. En cas de coupure, réutiliser le même identifiant et les mêmes données. Une demande active peut être annulée via `POST /v1/jobs/{id}/cancel`. En cas de quota dépassé, l’API retourne 429 et `Retry-After`.
6. Tester le retrait d’une invitation, la pause du poste, la déconnexion du réseau et la reprise d’un lot dans un environnement de test.

## Sauvegarde et reprise

Exécuter `python ops/backup.py <base-source> <nouvelle-copie>` depuis un environnement qui a accès au fichier SQLite et à son journal WAL. La commande utilise l’API de sauvegarde SQLite et contrôle l’intégrité de la copie ainsi que les clés étrangères. Stocker la copie chiffrée avec des droits restreints. Conserver les secrets et paramètres nécessaires séparément.

Restaurer d’abord vers un **nouvel emplacement** avec la même commande. Démarrer une instance isolée utilisant ce fichier et vérifier un projet, ses lots et l’historique. Documenter la durée de reprise et l’âge de la sauvegarde. Pour une reprise réelle, arrêter l’instance concernée avant de basculer le volume. Éviter d’écraser une base active.

## Conditions de passage à un pilote ouvert

| Chantier | Validation à obtenir |
|---|---|
| Charge scientifique | Capsule réelle approuvée, jeu de référence, tolérances et reproduction des résultats |
| Identités | Intégration SSO institutionnelle et séparation des établissements. Les comptes nominatifs, TOTP, rôles et la révocation des nœuds sont implémentés. |
| Agent Windows | Installateur signé et horodaté, mise à jour et désinstallation éprouvées, matrice des pilotes |
| Exploitation | Alertes, astreinte, rétention et politique de sauvegarde avec essai de restauration mesuré |
| Réseau et capacité | Essais multi-campus, incidents simulés et décision SQLite ou PostgreSQL fondée sur la charge |
| Données | Politique signée par les établissements, données de départ publiques ou synthétiques |
| Sécurité | Revue d’accès et de confinement, inventaire des composants et correctifs établis |
| Lancement | Critères d’acceptation chiffrés, responsable de décision et procédure de retour arrière |

La vérification serveur actuelle recalcule uniquement les carrés FP16 de la capsule de démonstration. Les capsules scientifiques nécessitent leur propre méthode de validation, et le pipeline LLM multi-nœuds nécessite une campagne distincte.
