# Programmateur YouTube (utilisable depuis l'iPad)

Tu envoies une vidéo + 2 phrases de description. Le site :
1. regarde tes anciennes vidéos et trouve ton meilleur jour/heure,
2. demande à Claude un titre dans le style de tes vidéos qui marchent,
3. envoie la vidéo sur YouTube, programmée à ce créneau.

## 1. Accès Google (console.cloud.google.com)
- Nouveau projet > activer **YouTube Data API v3**
- Écran de consentement OAuth : type Externe, ajoute ton email en "utilisateur test"
- Identifiants > ID client OAuth > **Application Web**
- URI de redirection autorisée : `https://TON-SITE/oauth/callback`

## 2. Hébergement (depuis l'iPad : render.com, gratuit)
- Mets ces fichiers dans un dépôt GitHub, puis Render > New Web Service
- Build : `pip install -r requirements.txt` — Start : voir `Procfile`
- Variables d'environnement :
  SECRET_KEY (texte aléatoire), APP_PASSWORD (ton mot de passe),
  BASE_URL (`https://TON-SITE.onrender.com`), GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
  GEMINI_API_KEY (aistudio.google.com, gratuit), TIMEZONE (optionnel)
- Ajoute un disque persistant et mets TOKEN_PATH=/data/token.json (sinon il faut reconnecter YouTube après chaque redémarrage)

## 3. Limites à connaître
- Les vidéos envoyées par un projet Google non vérifié restent **privées**, même programmées. Demande l'audit gratuit YouTube API (formulaire "YouTube API Services - Audit and Quota Extension").
- Quota gratuit : ~6 uploads/jour.
- L'IA ne voit pas les images : le titre vient de ta description.

## Version 2 (inspirée du bot d'un ami)
Pages : Publier, Calendrier, Bibliothèque, Stats, Réglages, et sélecteur de chaînes (+ Chaîne).
- **Apprentissage** : titres (liste à toi), hashtags et heures sont tirés au sort (Thompson sampling) ; après 8 vidéos publiées depuis plus de 48 h, le site favorise ce qui a le mieux marché. Tout est relu directement sur ta chaîne YouTube.
- Variables facultatives : `DATA_DIR` (dossier de stockage), `TITRES`, `HASHTAGS`, `HASHTAGS_TEST` (valeurs séparées par `|`, utilisées si tu n'as encore rien enregistré dans Réglages).
- Sur le plan gratuit de Render, les réglages et les connexions de chaînes sont effacés quand le site se réinitialise. Il suffit de les refaire (ou de mettre `TITRES`/`HASHTAGS` en variables pour garder une base).
