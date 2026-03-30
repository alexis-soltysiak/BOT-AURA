# Bot AURA

Bot Discord en Python qui attribue des points aux utilisateurs selon les reactions recues sur leurs messages.

## Regles

- 1 utilisateur qui reagit sur un message = 1 point pour l'auteur du message
- Plusieurs emojis differents par la meme personne sur le meme message comptent pour 1 seul point
- L'auto-reaction ne compte pas
- Les reactions ajoutees ou retirees plus tard sont prises en compte
- `/aura` affiche le classement des utilisateurs
- `/faker` affiche les 3 messages avec le plus de reactions uniques
- `/aura_rebuild` permet a un admin de rescanner l'historique du serveur
- `/aura_rebuild day:30 month:3 year:2026` permet de scanner seulement a partir d'une date

## Installation locale avec uv

1. Installer `uv`
2. Copier `.env.example` vers `.env`
3. Ajouter le token du bot Discord dans `.env`
4. Lancer :

```bash
uv sync
uv run bot-aura
```

Pour initialiser les scores sur l'historique existant du serveur, lance ensuite `/aura_rebuild` une fois.
Tu peux aussi limiter le scan avec `day`, `month` et `year` si tu veux repartir d'une date precise.

## Variables d'environnement

- `DISCORD_TOKEN` : token du bot Discord
- `COMMAND_SYNC_GUILD_ID` : optionnel, ID du serveur pour synchroniser instantanement les slash commands pendant le dev. Le bot doit deja etre present sur ce serveur.
- `AURA_REBUILD_ALLOWED_USER_ID` : optionnel, ID Discord autorise a utiliser `/aura_rebuild`
- `DATABASE_PATH` : chemin SQLite, par defaut `data/aura.sqlite3`
- `LOG_LEVEL` : niveau de logs, par defaut `INFO`
- `REBUILD_PAUSE_EVERY` : pause courte tous les N messages pendant `/aura_rebuild`, par defaut `50`
- `REBUILD_PAUSE_SECONDS` : duree de la pause pendant `/aura_rebuild`, par defaut `0.75`
- `REBUILD_PROGRESS_EVERY` : affiche une ligne de progression dans le terminal tous les N messages pendant `/aura_rebuild`, par defaut `100`

## Permissions et intents

Activer au minimum :

- `View Channels`
- `Read Message History`
- `Add Reactions`
- `Use Slash Commands`

Et cote portail Discord Developer :

- `Server Members Intent` n'est pas necessaire ici
- `Message Content Intent` n'est pas necessaire

## Deploiement Docker

```bash
docker compose up --build -d
```

La base SQLite est stockee dans `./data`.
