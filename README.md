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
  ANTHROPIC_API_KEY (console.anthropic.com), TIMEZONE (optionnel)
- Ajoute un disque persistant et mets TOKEN_PATH=/data/token.json (sinon il faut reconnecter YouTube après chaque redémarrage)

## 3. Limites à connaître
- Les vidéos envoyées par un projet Google non vérifié restent **privées**, même programmées. Demande l'audit gratuit YouTube API (formulaire "YouTube API Services - Audit and Quota Extension").
- Quota gratuit : ~6 uploads/jour.
- Claude ne voit pas les images : le titre vient de ta description.
