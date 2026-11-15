import os, json, tempfile, collections
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, session, redirect, render_template_string
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3
BASE = os.environ["BASE_URL"].rstrip("/")
PASSWORD = os.environ["APP_PASSWORD"]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
TOKEN = os.environ.get("TOKEN_PATH", "token.json")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

def flow():
    cfg = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"],
                   "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token"}}
    return Flow.from_client_config(cfg, SCOPES, redirect_uri=BASE + "/oauth/callback")

def creds():
    if not os.path.exists(TOKEN):
        return None
    c = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not c.valid and c.refresh_token:
        c.refresh(Request())
        open(TOKEN, "w").write(c.to_json())
    return c

def best_slot(yt):
    """Analyse les anciennes vidéos : vues/jour selon jour+heure de publication."""
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in yt.playlistItems().list(
        part="contentDetails", playlistId=up, maxResults=50).execute()["items"]]
    vids = yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute()["items"] if ids else []
    now = datetime.now(timezone.utc)
    score, titles = collections.defaultdict(list), []
    for v in vids:
        pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        views = int(v["statistics"].get("viewCount", 0))
        titles.append((views, v["snippet"]["title"]))
        age = (now - pub).total_seconds() / 86400
        if age >= 2:
            l = pub.astimezone(TZ)
            score[(l.weekday(), l.hour)].append(views / age ** 0.5)
    top = [t for _, t in sorted(titles, reverse=True)[:10]]
    if len(vids) < 5 or not score:
        wd, h = None, 18  # pas assez de données : 18h demain
        t = (datetime.now(TZ) + timedelta(days=1)).replace(hour=h, minute=0, second=0, microsecond=0)
        return t.astimezone(timezone.utc), top, "pas assez de vidéos : créneau par défaut 18h"
    wd, h = max(score, key=lambda k: sum(score[k]) / len(score[k]))
    t = datetime.now(TZ).replace(hour=h, minute=0, second=0, microsecond=0)
    while t.weekday() != wd or t < datetime.now(TZ) + timedelta(hours=1):
        t += timedelta(days=1) if t.weekday() != wd else timedelta(days=7)
        t = t.replace(hour=h)
    jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    return t.astimezone(timezone.utc), top, f"ton meilleur créneau : {jours[wd]} à {h}h"

def make_title(desc, top):
    """Titre via Gemini (clé gratuite). Si ça échoue : première ligne de la description."""
    fallback = (desc.strip().splitlines() or ["Nouvelle vidéo"])[0][:90]
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return fallback
    import urllib.request
    prompt = ("Voici les titres qui ont le mieux marché sur ma chaîne YouTube :\n- " + "\n- ".join(top)
              + f"\n\nDescription de ma nouvelle vidéo : {desc}\n\n"
              "Écris UN seul titre accrocheur (max 70 caractères), dans le même style et la même langue. "
              "Réponds uniquement par le titre.")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
        return r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')[:100]
    except Exception:
        return fallback

def authed(f):
    def w(*a, **k):
        return f(*a, **k) if session.get("ok") else redirect("/login")
    w.__name__ = f.__name__
    return w

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("pw") == PASSWORD:
            session["ok"] = True
            return redirect("/")
        err = "Mot de passe incorrect."
    return render_template_string(PAGE, view="login", err=err)

@app.route("/")
@authed
def home():
    return render_template_string(PAGE, view="home", linked=creds() is not None, msg=request.args.get("msg"))

@app.route("/connect")
@authed
def connect():
    f = flow()
    url, state = f.authorization_url(access_type="offline", prompt="consent")
    session["state"], session["cv"] = state, f.code_verifier
    return redirect(url)

@app.route("/oauth/callback")
@authed
def callback():
    f = flow()
    f.code_verifier = session.get("cv")
    f.fetch_token(authorization_response=BASE + request.full_path)
    open(TOKEN, "w").write(f.credentials.to_json())
    return redirect("/?msg=Chaîne connectée.")

@app.route("/upload", methods=["POST"])
@authed
def upload():
    c = creds()
    if not c:
        return redirect("/connect")
    yt = build("youtube", "v3", credentials=c)
    file = request.files["video"]
    desc = request.form.get("desc", "").strip()
    when, top, why = best_slot(yt)
    title = request.form.get("title", "").strip() or make_title(desc, top)
    body_desc = desc + ("\n\n#Shorts" if request.form.get("short") else "")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    file.save(tmp.name)
    body = {"snippet": {"title": title[:100], "description": body_desc, "categoryId": "22"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "publishAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
    media = MediaFileUpload(tmp.name, chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    os.unlink(tmp.name)
    local = when.astimezone(TZ).strftime("%d/%m à %Hh%M")
    return redirect(f"/?msg=Programmée le {local} — « {title} » ({why})")

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Programmateur YouTube</title><style>
:root{--bg:#f3efe6;--ink:#1d1b18;--mut:#6b655b;--line:#cfc7b8;--acc:#1f4d3a;--card:#fbf8f1}
@media(prefers-color-scheme:dark){:root{--bg:#16181a;--ink:#ece8df;--mut:#97928a;--line:#34383b;--acc:#7fd1a6;--card:#1d2023}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 Georgia,serif;
padding:max(24px,env(safe-area-inset-top)) 20px max(24px,env(safe-area-inset-bottom))}
main{max-width:560px;margin:0 auto}h1{font-size:2rem;line-height:1.15;margin:.5em 0 .3em}
p{color:var(--mut);margin:.2em 0 1.4em}label{display:block;font-weight:bold;margin:1.2em 0 .4em}
input,textarea{width:100%;font:inherit;padding:14px;border:1px solid var(--line);border-radius:10px;
background:var(--card);color:var(--ink)}textarea{min-height:110px}
.chk{display:flex;gap:10px;align-items:center;font-weight:normal}.chk input{width:22px;height:22px}
button,.btn{display:inline-block;width:100%;margin-top:1.6em;padding:16px;font:inherit;font-weight:bold;
background:var(--acc);color:var(--bg);border:0;border-radius:10px;text-align:center;text-decoration:none}
.msg{background:var(--card);border-left:4px solid var(--acc);padding:12px 14px;margin:1em 0}
.err{color:#b3261e}#w{display:none;margin-top:1em}
</style></head><body><main>
{% if view=='login' %}
<h1>Accès privé</h1><form method="post"><label>Mot de passe</label>
<input type="password" name="pw" autofocus><button>Entrer</button>
{% if err %}<p class="err">{{err}}</p>{% endif %}</form>
{% else %}
<h1>Poste ta vidéo, je choisis le titre et l’heure.</h1>
<p>Le créneau vient des performances de tes anciennes vidéos.</p>
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
{% if not linked %}<a class="btn" href="/connect">Connecter ma chaîne YouTube</a>
{% else %}
<form method="post" action="/upload" enctype="multipart/form-data" onsubmit="w.style.display='block';b.disabled=true;b.textContent='Envoi en cours…'">
<label>Vidéo ou Short</label><input type="file" name="video" accept="video/*" required>
<label>De quoi parle la vidéo ?</label><textarea name="desc" required placeholder="2-3 phrases : sujet, ce qu’on y voit, l’idée forte."></textarea>
<label>Titre (laisse vide pour que Claude le choisisse)</label><input name="title">
<label class="chk"><input type="checkbox" name="short" value="1"> C’est un Short</label>
<button id="b">Programmer</button><p id="w">Ne ferme pas cet onglet pendant l’envoi.</p></form>
{% endif %}{% endif %}
</main></body></html>"""
