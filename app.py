import os, json, math, random, tempfile, re, calendar, urllib.request
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
DATA = os.environ.get("DATA_DIR", "data")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.makedirs(DATA + "/chaines", exist_ok=True)
MIN_APPRENTISSAGE = 8  # en dessous : on explore au hasard

# ---------- chaînes (une par fichier) ----------
def _p(cid): return f"{DATA}/chaines/{cid}.json"
def charger(cid):
    return json.load(open(_p(cid))) if cid and os.path.exists(_p(cid)) else None
def sauver(cid, d): json.dump(d, open(_p(cid), "w"))
def liste():
    out = []
    for f in sorted(os.listdir(DATA + "/chaines")):
        if f.endswith(".json"):
            out.append((f[:-5], charger(f[:-5])["nom"]))
    return out
def active():
    ids = [c for c, _ in liste()]
    return session.get("cid") if session.get("cid") in ids else (ids[0] if ids else None)

def _env(k): return [x.strip() for x in os.environ.get(k, "").split("|") if x.strip()]
def config(cid):
    c = {"titres": _env("TITRES"), "fixes": _env("HASHTAGS"), "pool": _env("HASHTAGS_TEST"),
         "par_video": 8, "heures": [12, 18, 20], "apprentissage": True}
    c.update((charger(cid) or {}).get("config", {}))
    return c

def yt_for(cid):
    d = charger(cid)
    c = Credentials.from_authorized_user_info(json.loads(d["token"]), SCOPES)
    if not c.valid and c.refresh_token:
        c.refresh(Request())
        d["token"] = c.to_json(); sauver(cid, d)
    return build("youtube", "v3", credentials=c)

def flow():
    cfg = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"],
                   "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token"}}
    return Flow.from_client_config(cfg, SCOPES, redirect_uri=BASE + "/oauth/callback")

# ---------- historique YouTube (l'apprentissage se lit directement sur ta chaîne) ----------
def historique(yt):
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in yt.playlistItems().list(
        part="contentDetails", playlistId=up, maxResults=50).execute().get("items", [])]
    items = yt.videos().list(part="snippet,statistics,status", id=",".join(ids)).execute().get("items", []) if ids else []
    out = []
    for v in items:
        s, st, stt = v["snippet"], v.get("statistics", {}), v["status"]
        pub = stt.get("publishAt") or s["publishedAt"]
        out.append(dict(id=v["id"], titre=s["title"], desc=s.get("description", ""),
                        pub=datetime.fromisoformat(pub.replace("Z", "+00:00")),
                        vues=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                        statut=stt["privacyStatus"],
                        img=s.get("thumbnails", {}).get("medium", {}).get("url", "")))
    return sorted(out, key=lambda v: v["pub"], reverse=True)

def score(v): return math.log1p(v["vues"]) + 0.5 * math.log1p(v["likes"])

def mesurees(hist):
    lim = datetime.now(timezone.utc) - timedelta(hours=48)
    return [v for v in hist if v["statut"] == "public" and v["pub"] <= lim]

def stats_par(vids, cles):
    g = {}
    for v in vids:
        for k in cles(v):
            g.setdefault(k, []).append(score(v))
    return {k: (len(s), sum(s) / len(s)) for k, s in g.items()}

def globales(vids):
    s = [score(v) for v in vids]
    if not s: return 0.0, 1.0
    m = sum(s) / len(s)
    return m, max(math.sqrt(sum((x - m) ** 2 for x in s) / len(s)), 0.5)

def tirages(bras, st, m0, s0):
    """Thompson sampling : ce qui marche sort plus souvent, le peu testé reste exploré."""
    def t(b):
        n, m = st.get(b, (0, m0))
        return random.gauss((n * m + m0) / (n + 1), s0 / math.sqrt(n + 1))
    return sorted(bras, key=t, reverse=True)

def hashtags_de(v): return [h.lower() for h in re.findall(r"#\w+", v["desc"])]

def choisir(hist, cfg):
    vids = mesurees(hist)
    appr = cfg["apprentissage"] and len(vids) >= MIN_APPRENTISSAGE
    m0, s0 = globales(vids)
    titre = None
    if cfg["titres"]:
        cand = [t for t in cfg["titres"] if not hist or t != hist[0]["titre"]] or cfg["titres"]
        titre = tirages(cand, stats_par(vids, lambda v: [v["titre"]]), m0, s0)[0] if appr else random.choice(cand)
    fixes = [h.lower() if h.startswith("#") else "#" + h.lower() for h in cfg["fixes"]]
    pool = [h.lower() if h.startswith("#") else "#" + h.lower() for h in cfg["pool"]]
    pool = [h for h in dict.fromkeys(pool) if h not in fixes]
    n = max(0, int(cfg["par_video"]) - len(fixes))
    if appr: pool = tirages(pool, stats_par(vids, hashtags_de), m0, s0)
    else: random.shuffle(pool)
    heures = tirages(cfg["heures"], stats_par(vids, lambda v: [v["pub"].astimezone(TZ).hour]), m0, s0) if appr \
        else random.sample(cfg["heures"], len(cfg["heures"]))
    pris = {v["pub"].astimezone(TZ).date() for v in hist if v["pub"] > datetime.now(timezone.utc)}
    now = datetime.now(TZ)
    quand = None
    for d in range(0, 15):
        t = (now + timedelta(days=d)).replace(hour=int(heures[0]), minute=0, second=0, microsecond=0)
        if t >= now + timedelta(hours=1) and t.date() not in pris:
            quand = t; break
    quand = quand or (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return titre, fixes + pool[:n], quand.astimezone(timezone.utc), appr

def titre_ia(desc):
    fallback = (desc.strip().splitlines() or ["Nouvelle vidéo"])[0][:90]
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return fallback
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = f"Écris UN seul titre YouTube accrocheur (max 70 caractères) pour : {desc}\nRéponds uniquement par le titre."
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
        return r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')[:100]
    except Exception:
        return fallback

# ---------- accès ----------
def authed(f):
    def w(*a, **k):
        return f(*a, **k) if session.get("ok") else redirect("/login")
    w.__name__ = f.__name__
    return w

def vue(nom, **kw):
    return render_template_string(PAGE, view=nom, chaines=liste(), cid=active(), msg=request.args.get("msg"), **kw)

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("pw") == PASSWORD:
            session["ok"] = True
            return redirect("/")
        err = "Mot de passe incorrect."
    return render_template_string(PAGE, view="login", err=err, chaines=[], cid=None, msg=None)

@app.route("/canal/<cid>")
@authed
def canal(cid):
    session["cid"] = cid
    return redirect(request.args.get("retour", "/"))

@app.route("/")
@authed
def home():
    return vue("publier")

@app.route("/connect")
@authed
def connect():
    f = flow()
    url, state = f.authorization_url(access_type="offline", prompt="consent select_account")
    session["cv"] = f.code_verifier
    return redirect(url)

@app.route("/oauth/callback")
@authed
def callback():
    f = flow()
    f.code_verifier = session.get("cv")
    f.fetch_token(authorization_response=BASE + request.full_path)
    yt = build("youtube", "v3", credentials=f.credentials)
    ch = yt.channels().list(part="snippet", mine=True).execute()["items"][0]
    d = charger(ch["id"]) or {}
    d.update(nom=ch["snippet"]["title"], token=f.credentials.to_json())
    sauver(ch["id"], d)
    session["cid"] = ch["id"]
    return redirect("/?msg=Chaîne connectée : " + ch["snippet"]["title"])

@app.route("/upload", methods=["POST"])
@authed
def upload():
    cid = active()
    if not cid: return redirect("/connect")
    yt, cfg = yt_for(cid), config(cid)
    desc = request.form.get("desc", "").strip()
    titre, tags, when, appr = choisir(historique(yt), cfg)
    titre = request.form.get("title", "").strip() or titre or titre_ia(desc)
    texte = desc + ("\n\n" + " ".join(tags) if tags else "") + ("\n#Shorts" if request.form.get("short") else "")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    request.files["video"].save(tmp.name)
    body = {"snippet": {"title": titre[:100], "description": texte, "categoryId": "22"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "publishAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(tmp.name, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    os.unlink(tmp.name)
    mode = "choix appris" if appr else "phase de test (hasard)"
    return redirect(f"/?msg=Programmée le {when.astimezone(TZ):%d/%m à %Hh} — « {titre} » ({mode})")

@app.route("/calendrier")
@authed
def cal():
    cid = active()
    if not cid: return redirect("/")
    m = request.args.get("m") or datetime.now(TZ).strftime("%Y-%m")
    a, mo = map(int, m.split("-"))
    jours = {}
    for v in historique(yt_for(cid)):
        jours.setdefault(v["pub"].astimezone(TZ).date(), []).append(v)
    sem = calendar.Calendar().monthdatescalendar(a, mo)
    prec = (datetime(a, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    suiv = (datetime(a, mo, 28) + timedelta(days=5)).strftime("%Y-%m")
    return vue("cal", sem=sem, jours=jours, mois=m, mo=mo, prec=prec, suiv=suiv, now=datetime.now(timezone.utc))

@app.route("/bibliotheque")
@authed
def biblio():
    cid = active()
    if not cid: return redirect("/")
    return vue("biblio", vids=historique(yt_for(cid)), tz=TZ, now=datetime.now(timezone.utc))

@app.route("/stats")
@authed
def stats():
    cid = active()
    if not cid: return redirect("/")
    h = historique(yt_for(cid)); v = mesurees(h)
    top = lambda cles: sorted(((k, n, round(m, 2)) for k, (n, m) in stats_par(v, cles).items()),
                              key=lambda x: -x[2])[:5]
    return vue("stats", total=sum(x["vues"] for x in h), likes=sum(x["likes"] for x in h), n=len(h),
               nm=len(v), mini=MIN_APPRENTISSAGE, meilleures=sorted(h, key=lambda x: -x["vues"])[:5],
               heures=top(lambda x: [f"{x['pub'].astimezone(TZ).hour}h"]), titres=top(lambda x: [x["titre"]]),
               tags=top(hashtags_de))

@app.route("/reglages", methods=["GET", "POST"])
@authed
def reglages():
    cid = active()
    if not cid: return redirect("/")
    if request.method == "POST":
        f = request.form
        sp = lambda s: [x.strip() for x in re.split(r"[,\n]", s) if x.strip()]
        d = charger(cid)
        d["config"] = {"titres": [x.strip() for x in f["titres"].splitlines() if x.strip()],
                       "fixes": sp(f["fixes"]), "pool": sp(f["pool"]),
                       "par_video": int(f.get("par_video") or 8),
                       "heures": [int(x) for x in sp(f["heures"]) if x.isdigit() and int(x) < 24] or [18],
                       "apprentissage": bool(f.get("apprentissage"))}
        sauver(cid, d)
        return redirect("/reglages?msg=Réglages enregistrés.")
    return vue("reglages", c=config(cid))

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Programmateur YouTube</title><style>
:root{--bg:#f3efe6;--ink:#1d1b18;--mut:#6b655b;--line:#cfc7b8;--acc:#1f4d3a;--card:#fbf8f1}
@media(prefers-color-scheme:dark){:root{--bg:#16181a;--ink:#ece8df;--mut:#97928a;--line:#34383b;--acc:#7fd1a6;--card:#1d2023}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 Georgia,serif;
padding:max(20px,env(safe-area-inset-top)) 18px max(24px,env(safe-area-inset-bottom))}
main{max-width:760px;margin:0 auto}h1{font-size:1.8rem;line-height:1.15;margin:.6em 0 .3em}h2{font-size:1.15rem;margin:1.4em 0 .4em}
p{color:var(--mut);margin:.2em 0 1em}label{display:block;font-weight:bold;margin:1.1em 0 .4em}
input,textarea,select{width:100%;font:inherit;padding:13px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
textarea{min-height:110px}.chk{display:flex;gap:10px;align-items:center;font-weight:normal}.chk input{width:22px;height:22px}
button,.btn{display:inline-block;width:100%;margin-top:1.4em;padding:15px;font:inherit;font-weight:bold;background:var(--acc);color:var(--bg);border:0;border-radius:10px;text-align:center;text-decoration:none}
nav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}nav a{padding:8px 13px;border:1px solid var(--line);border-radius:20px;color:var(--ink);text-decoration:none;font-size:.95rem}
nav a.on{background:var(--acc);color:var(--bg);border-color:var(--acc)}
.msg{background:var(--card);border-left:4px solid var(--acc);padding:12px 14px;margin:1em 0}.err{color:#b3261e}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}.v{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:.9rem}
.v img{width:100%;display:block}.v div{padding:8px 10px}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:.85rem}td,th{border:1px solid var(--line);padding:5px;vertical-align:top;text-align:left}
td.p{background:var(--card)}.pub{color:var(--acc)}.prog{color:#b36b00}.st td{font-size:.95rem}
</style></head><body><main>
{% if view=='login' %}
<h1>Accès privé</h1><form method="post"><label>Mot de passe</label>
<input type="password" name="pw" autofocus><button>Entrer</button>{% if err %}<p class="err">{{err}}</p>{% endif %}</form>
{% else %}
<nav>{% for k,l in [('publier','Publier'),('cal','Calendrier'),('biblio','Bibliothèque'),('stats','Stats'),('reglages','Réglages')] %}
<a class="{{'on' if view==k}}" href="{{ {'publier':'/','cal':'/calendrier','biblio':'/bibliotheque','stats':'/stats','reglages':'/reglages'}[k] }}">{{l}}</a>{% endfor %}</nav>
<nav>{% for i,n in chaines %}<a class="{{'on' if i==cid}}" href="/canal/{{i}}?retour={{request.path}}">{{n}}</a>{% endfor %}<a href="/connect">+ Chaîne</a></nav>
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
{% if not cid %}<h1>Connecte ta chaîne YouTube</h1><a class="btn" href="/connect">Connecter ma chaîne</a>
{% elif view=='publier' %}
<h1>Poste ta vidéo, le site choisit titre, hashtags et heure.</h1>
<p>Les choix s’appuient sur les vues de tes vidéos passées. Les titres viennent de ta liste (Réglages).</p>
<form method="post" action="/upload" enctype="multipart/form-data" onsubmit="w.style.display='block';b.disabled=true;b.textContent='Envoi en cours…'">
<label>Vidéo ou Short</label><input type="file" name="video" accept="video/*" required>
<label>De quoi parle la vidéo ?</label><textarea name="desc" required></textarea>
<label>Titre (vide = tiré de ta liste, sinon IA)</label><input name="title">
<label class="chk"><input type="checkbox" name="short" value="1"> C’est un Short</label>
<button id="b">Programmer</button><p id="w" style="display:none">Ne ferme pas cet onglet pendant l’envoi.</p></form>
{% elif view=='cal' %}
<h1>Calendrier</h1><p><a href="/calendrier?m={{prec}}">Mois précédent</a> · {{mois}} · <a href="/calendrier?m={{suiv}}">Mois suivant</a></p>
<div style="overflow-x:auto"><table><tr>{% for j in ['L','M','M','J','V','S','D'] %}<th>{{j}}</th>{% endfor %}</tr>
{% for s in sem %}<tr>{% for d in s %}<td class="{{'p' if d.month==mo}}" style="{{'opacity:.4' if d.month!=mo}}"><b>{{d.day}}</b>
{% for v in jours.get(d,[]) %}<div class="{{'pub' if v.pub<now else 'prog'}}">{{v.titre[:28]}}</div>{% endfor %}</td>{% endfor %}</tr>{% endfor %}</table></div>
<p><span class="pub">Publiée</span> · <span class="prog">Programmée</span></p>
{% elif view=='biblio' %}
<h1>Bibliothèque</h1><div class="grid">{% for v in vids %}<div class="v"><img src="{{v.img}}" alt=""><div><b>{{v.titre}}</b><br>
<span class="mut">{{v.vues}} vues · {{v.likes}} j’aime<br>{{'Programmée' if v.pub>now else v.statut}} · {{v.pub.astimezone(tz).strftime('%d/%m %Hh')}}</span></div></div>{% endfor %}</div>
{% elif view=='stats' %}
<h1>Stats</h1><p>{{n}} vidéos récentes · {{total}} vues · {{likes}} j’aime</p>
<h2>Apprentissage</h2><p>{{nm}} vidéos mesurées sur {{mini}} nécessaires. {{'Actif : le site favorise ce qui marche.' if nm>=mini else 'Phase de test : titres, hashtags et heures varient au hasard.'}}</p>
{% for nom,l in [('Meilleures heures',heures),('Meilleurs titres',titres),('Meilleurs hashtags',tags)] %}<h2>{{nom}}</h2>
<table class="st"><tr><th>Valeur</th><th>Vidéos</th><th>Score moyen</th></tr>{% for k,n2,m in l %}<tr><td>{{k}}</td><td>{{n2}}</td><td>{{m}}</td></tr>{% endfor %}</table>{% endfor %}
<h2>Top vidéos</h2><table class="st">{% for v in meilleures %}<tr><td>{{v.titre}}</td><td>{{v.vues}} vues</td></tr>{% endfor %}</table>
{% elif view=='reglages' %}
<h1>Réglages</h1><form method="post">
<label>Tes titres (un par ligne)</label><textarea name="titres">{{c.titres|join('\n')}}</textarea>
<label>Hashtags fixes (séparés par des virgules)</label><input name="fixes" value="{{c.fixes|join(', ')}}">
<label>Hashtags à tester</label><textarea name="pool">{{c.pool|join(', ')}}</textarea>
<label>Hashtags par vidéo</label><input name="par_video" type="number" value="{{c.par_video}}">
<label>Heures de publication possibles (ex : 12, 18, 20)</label><input name="heures" value="{{c.heures|join(', ')}}">
<label class="chk"><input type="checkbox" name="apprentissage" {{'checked' if c.apprentissage}}> Apprendre de mes résultats</label>
<button>Enregistrer</button></form>
{% endif %}{% endif %}
</main></body></html>"""
