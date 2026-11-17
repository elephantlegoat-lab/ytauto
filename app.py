import os, json, math, random, tempfile, re, calendar, urllib.request, threading, uuid, statistics, time, base64, hmac, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, session, redirect, render_template_string, jsonify
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
LOGIN_FAILS = {}
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3
BASE = os.environ["BASE_URL"].rstrip("/")
PASSWORD = os.environ["APP_PASSWORD"]
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=BASE.startswith("https"),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
DATA = os.environ.get("DATA_DIR", "data")
SCOPES = ["https://www.googleapis.com/auth/youtube",
          "https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly",
          "https://www.googleapis.com/auth/drive.readonly"] + (
    ["https://www.googleapis.com/auth/gmail.send"] if os.environ.get("NOTIF_EMAIL") else [])
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.makedirs(DATA + "/chaines", exist_ok=True)
DEFAUT_TAGS = ["#asmr", "#shorts", "#asmrshorts", "#asmrman", "#fyp", "#satisfying", "#relax", "#relaxation",
               "#whispering", "#chuchotement", "#visualasmr", "#satisfyingvideo", "#frissonsgarantis", "#dormir", "#detente"]
OBLIG_TAGS = ["#asmr", "#shorts"]
EXTRA_TAGS = ["#asmrsleep", "#asmrtriggers", "#asmrsounds", "#tingles", "#relaxing", "#sleep", "#asmrcommunity",
              "#oddlysatisfying", "#asmrvideo", "#asmrtapping", "#asmrscratching", "#asmrfrance", "#asmrfr", "#triggers",
              "#brainmelt", "#asmrnomusic", "#calm", "#sleepaid", "#asmrlover", "#satisfyingsounds", "#crunchy",
              "#mouthsounds", "#asmrmic", "#tapping", "#relaxingsounds", "#stressrelief", "#chill", "#asmrfood", "#slime"]
NB_TAGS = 15
DEFAUT_HEURES = [12, 13, 18, 19, 20, 21, 22]
AVANCE_MIN = 5  # on publie 5 min avant l'heure de pointe
MIN_APPRENTISSAGE = 8  # en dessous : on explore au hasard

# ---------- chaînes (une par fichier) ----------
REDIS = bool(os.environ.get("UPSTASH_REDIS_REST_URL") and os.environ.get("UPSTASH_REDIS_REST_TOKEN"))
_MEM = {}

def _redis(*cmd):
    """Une commande Redis via l'API web d'Upstash (gratuit, données conservées)."""
    rq = urllib.request.Request(os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/"), data=json.dumps(list(cmd)).encode(),
                                headers={"Authorization": "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"],
                                         "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(rq, timeout=15)).get("result")

def _fichier(cle):
    return f"{DATA}/chaines/{cle[7:]}.json" if cle.startswith("chaine:") else f"{DATA}/{cle}.json"

def kv_get(cle):
    t, v = _MEM.get(cle, (0, None))
    if v is not None and time.time() - t < 120: return json.loads(v)
    if REDIS:
        try:
            raw = _redis("GET", cle)
            if raw:
                _MEM[cle] = (time.time(), raw); return json.loads(raw)
        except Exception: pass  # Upstash injoignable : on se rabat sur le fichier local
    try: return json.load(open(_fichier(cle)))
    except Exception: return None

def kv_set(cle, val):
    raw = json.dumps(val)
    _MEM[cle] = (time.time(), raw); _MEM.pop("__liste", None)
    try: open(_fichier(cle), "w").write(raw)
    except Exception: pass
    if REDIS:
        try:
            _redis("SET", cle, raw)
            if cle.startswith("chaine:"): _redis("SADD", "chaines", cle[7:])
        except Exception: pass

def charger(cid): return kv_get("chaine:" + cid) if cid else None
def sauver(cid, d): kv_set("chaine:" + cid, d)

def liste():
    t, v = _MEM.get("__liste", (0, None))
    if v is not None and time.time() - t < 30: return v
    ids = {f[:-5] for f in os.listdir(DATA + "/chaines") if f.endswith(".json")}
    if REDIS:
        try: ids |= set(_redis("SMEMBERS", "chaines") or [])
        except Exception: pass
    out = []
    for i in sorted(ids):
        d = kv_get("chaine:" + i)
        if d: out.append((i, d["nom"]))
    _MEM["__liste"] = (time.time(), out)
    return out

def etat_stockage():
    if not REDIS: return "temporaire : tout est effacé quand le site redémarre (branche Upstash pour tout garder)."
    try:
        _redis("PING"); return "permanent (Upstash) : connecté."
    except Exception as e:
        return "Upstash est configuré mais injoignable (" + str(e)[:90] + ") : données gardées seulement en temporaire."
def active():
    ids = [c for c, _ in liste()]
    return session.get("cid") if session.get("cid") in ids else (ids[0] if ids else None)

def _env(k): return [x.strip() for x in os.environ.get(k, "").split("|") if x.strip()]
def config(cid):
    c = {"titres": _env("TITRES"), "oblig": OBLIG_TAGS, "pool": _env("HASHTAGS_TEST"),
         "heures": DEFAUT_HEURES, "max_jour": 4, "apprentissage": True, "fond": "", "alerte_vues": 1000, "flow_auto": False, "drive_auto": False, "drive_dossier": "Flow"}
    c.update((charger(cid) or {}).get("config", {}))
    if not c["oblig"]: c["oblig"] = OBLIG_TAGS
    return c

def creds_for(cid):
    d = charger(cid)
    c = Credentials.from_authorized_user_info(json.loads(d["token"]))
    if not c.valid and c.refresh_token:
        c.refresh(Request())
        d["token"] = c.to_json(); sauver(cid, d)
    return c

def yt_for(cid): return build("youtube", "v3", credentials=creds_for(cid))

def flow():
    cfg = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"],
                   "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token"}}
    return Flow.from_client_config(cfg, SCOPES, redirect_uri=BASE + "/oauth/callback")

# ---------- historique YouTube (l'apprentissage se lit directement sur ta chaîne) ----------
def duree_s(d):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0) if m else 0

def historique(yt):
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok = [], None
    for _ in range(2):  # jusqu'à 100 vidéos
        r = yt.playlistItems().list(part="contentDetails", playlistId=up, maxResults=50,
                                    **({"pageToken": tok} if tok else {})).execute()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        tok = r.get("nextPageToken")
        if not tok: break
    items = []
    for k in range(0, len(ids), 50):
        items += yt.videos().list(part="snippet,statistics,status,contentDetails",
                                  id=",".join(ids[k:k + 50])).execute().get("items", [])
    out = []
    for v in items:
        s, st, stt = v["snippet"], v.get("statistics", {}), v["status"]
        pub = stt.get("publishAt") or s["publishedAt"]
        out.append(dict(id=v["id"], titre=s["title"], desc=s.get("description", ""),
                        pub=datetime.fromisoformat(pub.replace("Z", "+00:00")),
                        vues=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                        duree=duree_s(v.get("contentDetails", {}).get("duration")),
                        statut=stt["privacyStatus"], sched=bool(stt.get("publishAt")),
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

def heure_de(v):
    """Heure de pointe visée (une vidéo publiée à 17h55 compte pour 18h)."""
    return (v["pub"].astimezone(TZ) + timedelta(minutes=30)).hour

def occupation(hist):
    occ = {}
    for v in hist:
        occ.setdefault(v["pub"].astimezone(TZ).date(), []).append(heure_de(v))
    return occ

def prochain_creneau(occ, ranked, cap):
    """Premier jour qui a encore de la place (max `cap` par jour), à l'heure de pointe la mieux classée moins 5 min."""
    now = datetime.now(TZ)
    for d in range(0, 40):
        jour = (now + timedelta(days=d)).date()
        pris = occ.get(jour, [])
        if len(pris) >= cap: continue
        for h in ranked:
            if h in pris: continue
            t = datetime(jour.year, jour.month, jour.day, h, 0, tzinfo=TZ) - timedelta(minutes=AVANCE_MIN)
            if t >= now + timedelta(minutes=20):
                occ.setdefault(jour, []).append(h)
                return t
    return (now + timedelta(days=1)).replace(hour=17, minute=55, second=0, microsecond=0)

def hashtags_de(v): return [h.lower() for h in re.findall(r"#\w+", v["desc"])]

def choisir(hist, cfg, occ=None):
    vids = mesurees(hist)
    appr = cfg["apprentissage"] and len(vids) >= MIN_APPRENTISSAGE
    m0, s0 = globales(vids)
    titre = None
    if cfg["titres"]:
        cand = [t for t in cfg["titres"] if not hist or t != hist[0]["titre"]] or cfg["titres"]
        titre = tirages(cand, stats_par(vids, lambda v: [v["titre"]]), m0, s0)[0] if appr else random.choice(cand)
    norm = lambda h: h.lower() if h.startswith("#") else "#" + h.lower()
    oblig = [norm(h) for h in cfg["oblig"]]
    vus = [h for v in hist for h in hashtags_de(v)]
    cand = [h for h in dict.fromkeys([norm(h) for h in DEFAUT_TAGS + EXTRA_TAGS + cfg["pool"]] + vus) if h not in oblig]
    n = max(0, NB_TAGS - len(oblig))
    if appr:
        pris_tags = tirages(cand, stats_par(vids, hashtags_de), m0, s0)[:n]
    else:  # phase de test : on garde l'essentiel de ta liste et on teste 4 nouveaux hashtags
        base = [h for h in DEFAUT_TAGS if norm(h) not in oblig][:n]
        base = [norm(h) for h in base]
        random.shuffle(base)
        base = base[:max(0, len(base) - 4)]
        reste = [h for h in cand if h not in base]
        random.shuffle(reste)
        pris_tags = (base + reste)[:n]
    cand_h = list(dict.fromkeys(int(h) for h in cfg["heures"]))  # tes heures de pointe d'abord
    for h in DEFAUT_HEURES:  # on complète seulement s'il n'y en a pas assez pour remplir une journée
        if len(cand_h) >= int(cfg["max_jour"]): break
        if h not in cand_h: cand_h.append(h)
    if appr: ranked = tirages(cand_h, stats_par(vids, lambda v: [heure_de(v)]), m0, s0)
    else: ranked = random.sample(cand_h, len(cand_h))
    quand = prochain_creneau(occupation(hist) if occ is None else occ, ranked, int(cfg["max_jour"]))
    return titre, oblig + pris_tags, quand.astimezone(timezone.utc), appr

_GM = {"t": 0, "noms": []}

def _modeles_gemini(key):
    """Liste les modèles Gemini réellement disponibles pour ta clé (mise en mémoire 1 h)."""
    if _GM["noms"] and time.time() - _GM["t"] < 3600: return _GM["noms"]
    try:
        rq = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                    headers={"x-goog-api-key": key})
        L = json.load(urllib.request.urlopen(rq, timeout=15)).get("models", [])
        ok = [m["name"].split("/")[-1] for m in L if "generateContent" in m.get("supportedGenerationMethods", [])]
        bons = [n for n in ok if "flash" in n and not any(x in n for x in ("image", "tts", "live", "audio", "embed", "robotics", "computer"))]
        _GM.update(t=time.time(), noms=sorted(sorted(bons, reverse=True), key=lambda n: "preview" in n or "exp" in n))
    except Exception: pass
    return _GM["noms"]

def _appel(m, prompt, key, timeout):
    gc = {"maxOutputTokens": 800}
    if "2.5-flash" in m and "lite" not in m: gc["thinkingConfig"] = {"thinkingBudget": 0}  # réponse rapide, sans réflexion
    rq = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    r = json.load(urllib.request.urlopen(rq, timeout=timeout))
    return r["candidates"][0]["content"]["parts"][0]["text"].strip()

def gemini(prompt, max_essais=4):
    """Retourne (texte, erreur). Essaie ton modèle, puis ceux que ta clé permet vraiment d'utiliser."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return None, "clé GEMINI_API_KEY absente sur Render"
    errs, vus = [], set()
    for phase in (0, 1):
        noms = [os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash"] if phase == 0 else _modeles_gemini(key)[:3]
        for m in noms:
            if not m or m in vus or len(vus) >= max_essais: continue
            vus.add(m)
            try: return _appel(m, prompt, key, 20), ""
            except Exception as e: errs.append(f"{m} : {str(e)[:70]}")
    return None, " · ".join(errs) or "aucun modèle disponible"

def variante_locale(bons, out, deja):
    """Secours sans IA : mélange le début d'un de tes bons titres avec la fin d'un autre (jamais deux fois le même texte)."""
    crochets = ["à écouter au casque 🎧", "pour t’endormir 😴", "ultra relaxant ✨", "volume à fond 🔊", "(attention, frissons)"]
    sep = r"[:?!|]|\s[-–]\s"
    nk = lambda t: re.sub(r"[^\w\s]", "", t.lower()).strip()
    pris = {nk(o) for o in out} | {nk(d) for d in deja}
    for _ in range(60):
        a, b = random.sample(bons, 2) if len(bons) > 1 else (bons[0], bons[0])
        tete = re.split(sep, a)[0].strip()
        court = len(tete) < 14
        if court: tete = a.strip()  # début trop court (ex : « ASMR ») : on garde le titre entier
        fin = (re.split(sep, b)[1:] or [""])[0].strip()
        t = (f"{tete} : {fin}" if not court and fin and nk(fin) != nk(tete) and random.random() < .6 else f"{tete} {random.choice(crochets)}")[:90]
        if nk(t) not in pris: return t
    return f"{bons[0][:60]} {random.randint(2, 99)} 🎧"

def generer_titres(hist, cfg, n):
    """n titres DIFFÉRENTS, inspirés de tes meilleures vidéos. Retourne (titres, erreur_ia)."""
    pub = [v for v in hist if v["statut"] == "public"]
    mes = mesurees(hist)
    ref = sorted(mes if len(mes) >= 3 else pub, key=score, reverse=True)
    bons = [x["titre"] for x in ref[:8]]
    mauv = [x["titre"] for x in ref[-3:]] if len(ref) >= 8 else []
    deja = {x["titre"].lower() for x in hist}
    out = []
    p = ("Tu aides une chaîne YouTube Shorts ASMR francophone.\n"
         + ("Titres qui ont le MIEUX marché :\n- " + "\n- ".join(bons) + "\n" if bons else "")
         + ("Titres qui ont le MOINS marché :\n- " + "\n- ".join(mauv) + "\n" if mauv else "")
         + f"\nÉcris {n} titres DIFFÉRENTS les uns des autres, dans l'esprit des meilleurs (mots-clés, ton, longueur, structure) "
           "sans en copier aucun. Max 70 caractères chacun. Un titre par ligne, sans numéro, sans guillemets, rien d'autre.")
    txt, err = gemini(p)
    for l in (txt or "").splitlines():
        l = re.sub(r"[<>]", "", re.sub(r"<[^>]*>", "", re.sub(r"^[\s\-\d.)*•]+", "", l))).strip().strip('"“”*')
        if l and l.lower() not in deja and l.lower() not in {o.lower() for o in out}: out.append(l[:100])
    for t in cfg["titres"]:
        if len(out) >= n: break
        if t.lower() not in deja and t.lower() not in {o.lower() for o in out}: out.append(t)
    while len(out) < n:
        out.append(variante_locale(bons or ["ASMR relaxant pour dormir"], out, deja))
    return out[:n], err

JOBS = []
DRIVE_INFO, DRIVE_PAUSE = {}, {}

def prochain_9h():
    """Le quota YouTube repart à zéro vers 9h (heure de Paris)."""
    now = datetime.now(TZ); t = now.replace(hour=9, minute=5, second=0, microsecond=0)
    return (t if t > now else t + timedelta(days=1)).timestamp()

def _drive_etat(cid): return kv_get("drive:" + cid) or {"fait": [], "essais": {}}

def drive_marquer(cid, fid):
    d = _drive_etat(cid)
    if fid not in d["fait"]: d["fait"] = (d["fait"] + [fid])[-1000:]
    d["essais"].pop(fid, None); kv_set("drive:" + cid, d)

def drive_essai(cid, fid):
    d = _drive_etat(cid); d["essais"][fid] = d["essais"].get(fid, 0) + 1; kv_set("drive:" + cid, d)

def signature(p):
    """Empreinte rapide d'un fichier : taille + début + fin (suffit à repérer un même fichier)."""
    import hashlib
    n = os.path.getsize(p); h = hashlib.sha256(str(n).encode())
    with open(p, "rb") as f:
        h.update(f.read(1024 * 1024))
        if n > 2 * 1024 * 1024: f.seek(-1024 * 1024, 2); h.update(f.read())
    return h.hexdigest()

def deja_envoyes(cid): return kv_get("sig:" + cid) or []

def noter_envoi(cid, sig, nom):
    L = [x for x in deja_envoyes(cid) if x.get("sig") != sig]
    L.append(dict(sig=sig, nom=nom, le=datetime.now(TZ).strftime("%d/%m")))
    kv_set("sig:" + cid, L[-500:])

def traiter(cid, jobs):
    """Tourne en arrière-plan : envoie chaque vidéo, vérifie qu'YouTube l'a bien reçue."""
    try:
        yt, cfg = yt_for(cid), config(cid)
        hist = historique(yt); occ = occupation(hist)
        titres, err_ia = generer_titres(hist, cfg, len(jobs))
        for j, titre in zip(jobs, titres):
            j.update(etat="envoi", titre=re.sub(r"[<>]", "", j.get("force") or titre), ia=err_ia)
            try:
                _, tags, when, appr = choisir(hist, cfg, occ)
                body = {"snippet": {"title": j["titre"][:100], "description": " ".join(tags), "categoryId": "22"},
                        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                                   "publishAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
                req = yt.videos().insert(part="snippet,status", body=body,
                                         media_body=MediaFileUpload(j["path"], chunksize=8 * 1024 * 1024, resumable=True))
                resp = None
                while resp is None:
                    st, resp = req.next_chunk()
                    if st: j["pct"] = int(st.progress() * 100)
                v = yt.videos().list(part="status", id=resp["id"]).execute().get("items", [])
                flow_marquer(cid, [resp["id"]])
                if not v: raise Exception("YouTube ne retrouve pas la vidéo après l'envoi.")
                stt = v[0]["status"]
                warn = "" if stt.get("publishAt") else \
                    "YouTube n'a pas gardé la programmation (projet Google non validé) : la vidéo reste privée."
                if j.get("sig"): noter_envoi(cid, j["sig"], j["nom"])
                if j.get("drive_id"): drive_marquer(cid, j["drive_id"])
                j.update(etat="ok", pct=100, vid=resp["id"], warn=warn,
                         quand=f"{when.astimezone(TZ):%d/%m à %Hh%M}")
                hist.insert(0, dict(id=resp["id"], titre=j["titre"], desc=" ".join(tags), pub=when, vues=0, likes=0,
                                    duree=0, statut="private", img=""))
            except Exception as e:
                if "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
                    j.update(etat="erreur", msg="Limite d'envoi YouTube atteinte pour aujourd'hui (environ 6 vidéos par jour). "
                                                "Réessaie demain vers 9h : envoie celle-ci à nouveau.")
                    for k in jobs:
                        if k["etat"] == "attente": k.update(etat="erreur", msg=j["msg"])
                    DRIVE_PAUSE[cid] = prochain_9h()  # les fichiers restent dans le dossier Drive, on réessaiera après 9h
                    break
                j.update(etat="erreur", msg=str(e)[:300])
                if j.get("drive_id"): drive_essai(cid, j["drive_id"])
    except Exception as e:
        for j in jobs:
            if j["etat"] == "attente": j.update(etat="erreur", msg=str(e)[:300])
    finally:
        for j in jobs:
            if os.path.exists(j["path"]): os.unlink(j["path"])
        CACHE.pop(("h", cid), None); CACHE.pop(("hist", cid), None)

# ---------- dossier Google Drive : tout ce qu'on y dépose est programmé tout seul ----------
def drive_auto(cid, nom):
    cfg = config(cid)
    if not cfg.get("drive_auto"): return
    if time.time() < DRIVE_PAUSE.get(cid, 0):
        DRIVE_INFO[cid] = "En pause : limite d'envoi YouTube atteinte, reprise vers 9h. Tes vidéos restent dans le dossier."; return
    dr = build("drive", "v3", credentials=creds_for(cid))
    dossier = (cfg.get("drive_dossier") or "Flow").strip()
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false and name='" + dossier.replace("\\", "\\\\").replace("'", "\\'") + "'"
    r = dr.files().list(q=q, fields="files(id,name)", pageSize=5).execute().get("files", [])
    if not r:
        DRIVE_INFO[cid] = f"Dossier « {dossier} » introuvable dans ton Google Drive (vérifie le nom)."; return
    fs = dr.files().list(q=f"'{r[0]['id']}' in parents and mimeType contains 'video/' and trashed=false",
                         fields="files(id,name,size,createdTime)", orderBy="createdTime", pageSize=50).execute().get("files", [])
    d = _drive_etat(cid); en_cours = {j.get("drive_id") for j in JOBS if j.get("etat") in ("attente", "envoi")}
    abandon = [f for f in fs if d["essais"].get(f["id"], 0) >= 3 and f["id"] not in d["fait"]]
    neufs = [f for f in fs if f["id"] not in d["fait"] and f["id"] not in en_cours and d["essais"].get(f["id"], 0) < 3]
    DRIVE_INFO[cid] = (f"Dernière vérification à {datetime.now(TZ):%Hh%M} : {len(neufs)} nouvelle(s) vidéo(s) dans « {dossier} »."
                       + (f" {len(abandon)} vidéo(s) abandonnée(s) après 3 échecs ({', '.join(f['name'] for f in abandon)[:80]})." if abandon else ""))
    if neufs: threading.Thread(target=_drive_traiter, args=(cid, nom, neufs[:6]), daemon=True).start()

def _drive_traiter(cid, nom, fichiers):
    jobs = []
    try:
        dr = build("drive", "v3", credentials=creds_for(cid)); deja = {x["sig"] for x in deja_envoyes(cid)}
        for f in fichiers:
            p = os.path.join(tempfile.gettempdir(), uuid.uuid4().hex + ".mp4")
            try:
                with open(p, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, dr.files().get_media(fileId=f["id"]), chunksize=8 * 1024 * 1024); fini = False
                    while not fini: _, fini = dl.next_chunk()
                if os.path.getsize(p) == 0: raise Exception("fichier vide")
                sig = signature(p)
                if sig in deja:  # déjà publiée : on ne la renvoie pas
                    drive_marquer(cid, f["id"]); os.unlink(p); continue
                jobs.append(dict(sig=sig, nom=f["name"], path=p, etat="attente", pct=0, titre="", msg="", warn="", vid="", quand="", ia="", drive_id=f["id"]))
            except Exception as e:
                drive_essai(cid, f["id"])
                if os.path.exists(p): os.unlink(p)
        if jobs:
            JOBS[:] = JOBS[-30:] + jobs
            traiter(cid, jobs)
            ok = [j for j in jobs if j["etat"] == "ok"]; ko = [j for j in jobs if j["etat"] != "ok"]
            if moyens():
                txt = "\n".join([f"{nom} : {len(ok)} vidéo(s) programmée(s) depuis ton dossier Drive."] + [f"• {j['titre'][:45]} ({j['quand']})" for j in ok]
                                 + [f"Échec : {j['nom']} ({j['msg'][:60]})" for j in ko])
                notifier("Vidéos programmées depuis Drive", txt, cid)
    except Exception as e:
        DRIVE_INFO[cid] = "Problème : " + erreur_claire(e)[0]
        for j in jobs:
            if os.path.exists(j["path"]): os.unlink(j["path"])

# ---------- vidéos venues de Google Flow ----------
FLOW_INFO = {}

def _flow_ids(cid): return kv_get("flow:" + cid) or {"ids": [], "base": False}

def flow_marquer(cid, ids):
    """Vidéos déjà traitées (ou envoyées par le site) : le pont Flow n'y touche jamais."""
    d = _flow_ids(cid); d["ids"] = (d["ids"] + [i for i in ids if i not in d["ids"]])[-1000:]; kv_set("flow:" + cid, d)

def flow_auto(cid, nom):
    """Une vidéo envoyée en privé depuis Flow (moins de 24 h, pas programmée) reçoit un titre IA, 15 hashtags et un créneau."""
    cfg = config(cid)
    if not cfg.get("flow_auto"): return []
    yt = yt_for(cid); hist = historique(yt); d = _flow_ids(cid); now = datetime.now(timezone.utc)
    age = lambda v: (now - v["pub"]).total_seconds() / 3600
    if not d["base"]:  # 1re vérification : tout ce qui existe déjà est mis de côté, on ne touche qu'aux NOUVELLES vidéos
        d["ids"] = list(dict.fromkeys(d["ids"] + [v["id"] for v in hist if age(v) > 0.25 or v["pub"] > now])); d["base"] = True
        kv_set("flow:" + cid, d); FLOW_INFO[cid] = "Activé. Surveille les nouvelles vidéos privées envoyées depuis Flow."; return []
    cand = [v for v in hist if v["statut"] == "private" and not v.get("sched") and v["id"] not in d["ids"]
            and 0 < v["duree"] <= 180 and 0 < age(v) <= 24]
    FLOW_INFO[cid] = f"Dernière vérification à {datetime.now(TZ):%Hh%M} : {len(cand)} vidéo(s) à programmer."
    if not cand: return []
    occ = occupation(hist); titres, _ = generer_titres(hist, cfg, len(cand)); faits = []
    for v, titre in zip(cand, titres):
        try:
            it = yt.videos().list(part="snippet,status", id=v["id"]).execute()["items"][0]
            sn, st = it["snippet"], it["status"]
            _, tags, when, _ = choisir(hist, cfg, occ)
            titre = re.sub(r"[<>]", "", titre)[:100]
            snippet = {"title": titre, "description": " ".join(tags), "categoryId": sn.get("categoryId", "22")}
            for k in ("tags", "defaultLanguage"):
                if sn.get(k): snippet[k] = sn[k]
            statut = {k: st[k] for k in ("embeddable", "license", "publicStatsViewable") if k in st}
            statut.update(privacyStatus="private", publishAt=when.strftime("%Y-%m-%dT%H:%M:%S.000Z"), selfDeclaredMadeForKids=False)
            yt.videos().update(part="snippet,status", body={"id": v["id"], "snippet": snippet, "status": statut}).execute()
            flow_marquer(cid, [v["id"]])
            hist.insert(0, dict(v, titre=titre, desc=" ".join(tags), pub=when, sched=True))
            faits.append(f"{nom} : « {titre[:50]} » programmée le {when.astimezone(TZ):%d/%m à %Hh%M}")
        except Exception as e:
            FLOW_INFO[cid] = "Problème : " + erreur_claire(e)[0]
    return faits

SOURCES = {"SHORTS": "Shorts", "YT_SEARCH": "Recherche YouTube", "SUGGESTED_VIDEO": "Vidéos suggérées",
           "BROWSE": "Accueil YouTube", "EXT_URL": "Sites externes", "NO_LINK_OTHER": "Direct / autre",
           "NOTIFICATION": "Notifications", "YT_CHANNEL": "Page de chaîne", "PLAYLIST": "Playlists",
           "YT_OTHER_PAGE": "Autres pages YouTube", "END_SCREEN": "Fin de vidéo", "HASHTAGS": "Hashtags"}

def analytique(cid, yt, j):
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    fin = datetime.now(TZ).date()
    try:  # on s'aligne sur le dernier jour réellement confirmé par YouTube (sinon les comparaisons sont faussées)
        r10 = ya.reports().query(ids="channel==MINE", startDate=str(fin - timedelta(days=10)), endDate=str(fin),
                                 metrics="views", dimensions="day", sort="day").execute().get("rows", [])
        if r10:
            fin = datetime.fromisoformat(r10[-1][0]).date()
            if len(r10) >= 4 and r10[-1][1] < 0.5 * sum(r[1] for r in r10[-4:-1]) / 3:
                fin -= timedelta(days=1)  # dernier jour encore incomplet
    except Exception: pass
    deb = fin - timedelta(days=j)
    q = lambda **k: ya.reports().query(ids="channel==MINE", startDate=str(deb), endDate=str(fin), **k).execute().get("rows", [])
    jour = q(metrics="views,estimatedMinutesWatched,likes,subscribersGained,subscribersLost", dimensions="day", sort="day")
    top = q(metrics="views,estimatedMinutesWatched", dimensions="video", sort="-views", maxResults=8)
    src = q(metrics="views", dimensions="insightTrafficSourceType", sort="-views", maxResults=8)
    noms = {}
    if top:
        for x in yt.videos().list(part="snippet", id=",".join(r[0] for r in top)).execute().get("items", []):
            noms[x["id"]] = x["snippet"]["title"]
    S = lambda i: [r[i] for r in jour]
    tot_ = dict(vues=sum(S(1)), heures=round(sum(S(2)) / 60, 1), likes=sum(S(3)), abos=sum(S(4)) - sum(S(5)))
    pv = ya.reports().query(ids="channel==MINE", startDate=str(deb - timedelta(days=j + 1)), endDate=str(deb - timedelta(days=1)),
                            metrics="views,estimatedMinutesWatched,likes,subscribersGained,subscribersLost").execute().get("rows") or [[0] * 5]
    prev = dict(vues=pv[0][0], heures=round(pv[0][1] / 60, 1), likes=pv[0][2], abos=pv[0][3] - pv[0][4])
    delta = {k: (round((tot_[k] - prev[k]) / abs(prev[k]) * 100) if prev[k] else None) for k in prev}
    return dict(labels=[r[0][8:] + "/" + r[0][5:7] for r in jour], vues=S(1), heures=[round(m / 60, 2) for m in S(2)],
                likes=S(3), abos=[a - b for a, b in zip(S(4), S(5))],
                tot=dict(vues=sum(S(1)), heures=round(sum(S(2)) / 60, 1), likes=sum(S(3)), abos=sum(S(4)) - sum(S(5))),
                delta=delta, fin=fin.strftime('%d/%m'), top=[(noms.get(r[0], r[0]), r[1], round(r[2] / 60, 1)) for r in top],
                src=[(SOURCES.get(r[0], r[0]), r[1]) for r in src])

def barres(mon):
    cibles = [("Vues Shorts sur 90 jours", mon["shorts"], 10_000_000), ("Abonnés", mon["subs"], 1000),
              ("Heures vues sur 12 mois (autre voie)", mon["heures"], 4000)]
    return [(n, v, c, min(100.0, round(v / c * 100, 1))) for n, v, c in cibles]

def monetisation(cid, yt):
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    fin = datetime.now(TZ).date()
    q = lambda j, **k: ya.reports().query(ids="channel==MINE", startDate=str(fin - timedelta(days=j)),
                                          endDate=str(fin), **k).execute().get("rows", [])
    shorts = sum(r[1] for r in q(90, metrics="views", dimensions="creatorContentType") if r[0] == "SHORTS")
    heures = sum(r[0] for r in q(365, metrics="estimatedMinutesWatched")) / 60
    subs = int(yt.channels().list(part="statistics", mine=True).execute()["items"][0]["statistics"].get("subscriberCount", 0))
    return dict(shorts=shorts, heures=round(heures), subs=subs)

def retention(cid):
    try:
        ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
        fin = datetime.now(TZ).date()
        rows = ya.reports().query(ids="channel==MINE", startDate=str(fin - timedelta(days=90)), endDate=str(fin),
                                  metrics="views,averageViewPercentage", dimensions="video", sort="-views",
                                  maxResults=200).execute().get("rows", [])
        return {r[0]: dict(ret=r[2]) for r in rows}
    except Exception:
        return {}

def analyser(hist, ret, seuil):
    """Compare tes vidéos sous le seuil (vues/jour) à tes meilleures et cherche pourquoi elles n'ont pas percé."""
    now = datetime.now(timezone.utc); rows = []
    for v in hist:
        age = (now - v["pub"]).total_seconds() / 86400
        if v["statut"] != "public" or age < 2: continue
        rows.append(dict(v, vpj=v["vues"] / min(age, 7), age=age, nb_tags=len(hashtags_de(v)), long_t=len(v["titre"]),
                         q="?" in v["titre"], asmr="asmr" in v["titre"].lower(),
                         jlike=v["likes"] / max(v["vues"], 1) * 100, ret=ret.get(v["id"], {}).get("ret"),
                         h=heure_de(v)))
    if len(rows) < 4: return None
    bons = [r for r in rows if r["vpj"] >= seuil]
    ref_seuil = len(bons) >= 3
    if not ref_seuil: bons = sorted(rows, key=lambda r: -r["vpj"])[:max(3, len(rows) // 4)]
    bas = sorted([r for r in rows if r["vpj"] < seuil and r not in bons], key=lambda r: r["vpj"])
    ref_bas = bas or [r for r in rows if r not in bons]
    med = lambda g, k: statistics.median([r[k] for r in g if r[k] is not None] or [0]) if any(r[k] is not None for r in g) else None
    part = lambda g, k: sum(1 for r in g if r[k]) / max(len(g), 1)
    cles = [("Longueur du titre (caractères)", "long_t"), ("Durée (secondes)", "duree"), ("Hashtags", "nb_tags"),
            ("J'aime pour 100 vues", "jlike"), ("Rétention moyenne (%)", "ret")]
    fm = lambda x: "?" if x is None else f"{x:.1f}" if x < 100 else f"{x:.0f}"
    comp = [(l, fm(med(bons, k)), fm(med(ref_bas, k))) for l, k in cles]
    comp += [("Titre avec « ASMR »", f"{part(bons, 'asmr') * 100:.0f} %", f"{part(ref_bas, 'asmr') * 100:.0f} %"),
             ("Titre avec une question", f"{part(bons, 'q') * 100:.0f} %", f"{part(ref_bas, 'q') * 100:.0f} %")]
    mes = {k: med(bons, k) for _, k in cles}
    th = {}
    for r in rows:
        for h in set(hashtags_de(r)): th.setdefault(h, []).append(r["vpj"])
    lift = sorted(((h, len(l), round(sum(l) / len(l))) for h, l in th.items() if 2 <= len(l) < len(rows)), key=lambda x: -x[2])
    tags_bons, tags_mauv = lift[:5], (lift[-5:][::-1] if len(lift) > 5 else [])
    hh = {}
    for r in rows: hh.setdefault(r["h"], []).append(r["vpj"])
    hs = sorted(((h, len(l), round(sum(l) / len(l))) for h, l in hh.items() if len(l) >= 2), key=lambda x: -x[2])
    pires = {h for h, _, _ in hs[-2:]} if len(hs) >= 4 else set()
    mauv = {h for h, _, _ in tags_mauv}
    for r in bas:
        w = []
        if mes["duree"] and r["duree"] > mes["duree"] * 1.3:
            w.append(f"Trop longue : {r['duree']} s contre {mes['duree']:.0f} s chez tes meilleures.")
        if r["ret"] is not None and mes["ret"] and r["ret"] < mes["ret"] * 0.85:
            w.append(f"Rétention faible : {r['ret']:.0f} % regardés (tes meilleures : {mes['ret']:.0f} %). Les premières secondes n'accrochent pas assez.")
        if mes["long_t"] and abs(r["long_t"] - mes["long_t"]) > mes["long_t"] * 0.4:
            w.append(f"Titre {'trop long' if r['long_t'] > mes['long_t'] else 'trop court'} ({r['long_t']} caractères, tes meilleurs : ~{mes['long_t']:.0f}).")
        if not r["asmr"] and part(bons, "asmr") > 0.6: w.append("Pas de « ASMR » dans le titre, alors que la plupart de tes meilleures vidéos l'ont.")
        if not r["q"] and part(bons, "q") > 0.4: w.append("Pas de question dans le titre, alors que tes meilleures en ont souvent une.")
        if mes["jlike"] and r["jlike"] < mes["jlike"] * 0.6: w.append(f"Peu de j'aime ({r['jlike']:.1f} pour 100 vues contre {mes['jlike']:.1f}) : le contenu donne peu envie de réagir.")
        m2 = [h for h in hashtags_de(r) if h in mauv]
        if m2: w.append("Hashtags qui tirent vers le bas chez toi : " + " ".join(m2) + ".")
        if r["h"] in pires: w.append(f"Publiée vers {r['h']}h, une de tes pires heures.")
        if r["age"] < 7: w.append("Encore récente : l'algorithme peut encore la pousser.")
        r["why"] = w[:5] or ["Aucun écart net avec tes meilleures vidéos : le sujet, le son ou la miniature jouent sûrement."]
    cs = []
    a, b = mes["duree"], med(ref_bas, "duree")
    if a and b and b > a * 1.25: cs.append(f"Durée : tes meilleures vidéos font ~{a:.0f} s, les autres ~{b:.0f} s. Raccourcis.")
    a, b = mes["ret"], med(ref_bas, "ret")
    if a and b and b < a * 0.85: cs.append(f"Rétention : {a:.0f} % pour tes meilleures contre {b:.0f} %. Va droit au son principal dès la première seconde.")
    a, b = mes["long_t"], med(ref_bas, "long_t")
    if a and b and abs(a - b) > a * 0.25: cs.append(f"Titres : les meilleurs font ~{a:.0f} caractères, les autres ~{b:.0f}.")
    if tags_mauv: cs.append("À retirer : " + " ".join(h for h, _, _ in tags_mauv) + ".")
    if tags_bons: cs.append("À garder : " + " ".join(h for h, _, _ in tags_bons) + ".")
    if hs: cs.append("Tes meilleures heures : " + ", ".join(f"{h}h" for h, _, _ in hs[:3]) + ".")
    return dict(n=len(rows), nb_bons=sum(1 for r in rows if r["vpj"] >= seuil), ref_seuil=ref_seuil, nb_ref=len(bons), bas=bas[:30], comp=comp, conseils=cs, tags_bons=tags_bons,
                tags_mauv=tags_mauv, heures=hs, top=sorted(rows, key=lambda r: -r["vpj"])[:8])

CACHE, BADGE = {}, {}
JOURS = ["LUN.", "MAR.", "MER.", "JEU.", "VEN.", "SAM.", "DIM."]
ICONS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "upload": '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v4h16v-4"/>',
    "chart": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
    "cal": '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
    "sliders": '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'}

@app.template_filter("k")
def _k(n):
    if isinstance(n, float) and abs(n) < 1000: return f"{n:.1f}".rstrip("0").rstrip(".") or "0"
    n = int(n or 0)
    if abs(n) >= 1_000_000: return f"{n / 1e6:.1f} M".replace(".0", "")
    if abs(n) >= 1000: return f"{n / 1000:.1f} k".replace(".0", "")
    return str(n)

def cache(cle, ttl, fn):
    t, v = CACHE.get(cle, (0, None))
    if v is not None and time.time() - t < ttl: return v
    v = fn(); CACHE[cle] = (time.time(), v); return v

_REFRESH = set()

def cache_sr(cle, ttl, fn, vieux=1800):
    """Réponse instantanée : on donne tout de suite ce qu'on a en mémoire (même un peu ancien)
    et on le rafraîchit en arrière-plan. Au-delà de `vieux` secondes, on recalcule pour de bon."""
    t, v = CACHE.get(cle, (0, None))
    if v is None or time.time() - t > vieux:
        v = fn(); CACHE[cle] = (time.time(), v); return v
    if time.time() - t >= ttl and cle not in _REFRESH:
        _REFRESH.add(cle)
        def maj():
            try: CACHE[cle] = (time.time(), fn())
            except Exception: pass
            finally: _REFRESH.discard(cle)
        threading.Thread(target=maj, daemon=True).start()
    return v

def hist_de(cid, ttl=60):
    return cache_sr(("hist", cid), ttl, lambda: historique(yt_for(cid)))

_CHAUF = threading.Lock()

def chauffer():
    """Prépare à l'avance les données de l'accueil : les pages s'ouvrent alors tout de suite."""
    if not _CHAUF.acquire(blocking=False): return
    try:
        for cid, _ in liste():
            try: CACHE[("h", cid)] = (time.time(), donnees_chaine(cid))
            except Exception: pass
    finally: _CHAUF.release()

@app.after_request
def pas_de_cache(r):
    if r.mimetype == "text/html": r.headers["Cache-Control"] = "no-store"
    r.headers.setdefault("X-Content-Type-Options", "nosniff"); r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r

def recent(cid, yt):
    """Vues arrivées depuis le dernier jour confirmé par YouTube Analytics (estimation : Analytics a 2 à 3 jours de retard)."""
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    ch = yt.channels().list(part="snippet,statistics", mine=True).execute()["items"][0]
    debut, live = ch["snippet"]["publishedAt"][:10], int(ch["statistics"].get("viewCount", 0))
    fin = datetime.now(TZ).date()
    q = lambda d0, **k: ya.reports().query(ids="channel==MINE", startDate=d0, endDate=str(fin), **k).execute().get("rows", [])
    jours = q(str(fin - timedelta(days=10)), metrics="views", dimensions="day", sort="day")
    total = q(debut, metrics="views")
    if not jours or not total: return None
    d = datetime.fromisoformat(jours[-1][0]).date(); lag = max(0, (fin - d).days)
    moy = sum(r[1] for r in jours[-7:]) / len(jours[-7:])
    est = max(0, live - total[0][0])
    if est > moy * (lag + 2) * 3 + 200: return None  # écart trop grand : les deux compteurs ne se comparent pas
    return dict(dernier=d.strftime("%d/%m"), jours=lag, est=est)

def vues_28j(cid):
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    fin = datetime.now(TZ).date()
    rows = ya.reports().query(ids="channel==MINE", startDate=str(fin - timedelta(days=28)), endDate=str(fin),
                              metrics="views").execute().get("rows", [])
    return rows[0][0] if rows else 0

def donnees_chaine(cid):
    """Tout ce qu'il faut pour l'accueil d'une chaîne (les appels à Google partent en même temps)."""
    from concurrent.futures import ThreadPoolExecutor
    cfg = config(cid)
    creds_for(cid)  # rafraîchit le jeton une seule fois avant de partir en parallèle
    def t_hist(): return historique(yt_for(cid))
    def t_chaine(): return yt_for(cid).channels().list(part="snippet,statistics", mine=True).execute()["items"][0]
    def t_28j():
        try: return vues_28j(cid)
        except Exception: return None
    def t_recent():
        try: return recent(cid, yt_for(cid))
        except Exception: return None
    with ThreadPoolExecutor(4) as ex:
        fs = [ex.submit(f) for f in (t_hist, t_chaine, t_28j, t_recent)]
        hist, ch, v28, r = [f.result() for f in fs]
    CACHE[("hist", cid)] = (time.time(), hist)
    st = ch["statistics"]; now = datetime.now(timezone.utc); aujourd = datetime.now(TZ).date()
    loc = lambda v: v["pub"].astimezone(TZ)
    futur = sorted((v for v in hist if v["pub"] > now), key=lambda v: v["pub"])
    if v28 is not None and r: v28 += r["est"]
    def jour_txt(d): return "aujourd’hui" if d == aujourd else "demain" if d == aujourd + timedelta(days=1) else f"{d:%d/%m}"
    sem = []
    for i in range(7):
        j = aujourd + timedelta(days=i)
        hs = sorted(f"{loc(v):%Hh%M}" for v in hist if loc(v).date() == j)
        sem.append(dict(jour=j, lab=JOURS[j.weekday()], n=len(hs), heures=hs))
    BADGE[cid] = len(futur)
    return dict(cid=cid, nom=ch["snippet"]["title"], img=ch["snippet"]["thumbnails"].get("default", {}).get("url", ""),
                abos=int(st.get("subscriberCount", 0)), vues=int(st.get("viewCount", 0)), vues28=v28,
                aujourd=sum(1 for v in hist if loc(v).date() == aujourd), max=int(cfg["max_jour"]), prog=len(futur), sem=sem, stock=stock(hist, cfg),
                futur=[dict(titre=v["titre"], quand=loc(v), iso=v["pub"].isoformat(), jour=jour_txt(loc(v).date()), id=v["id"], cid=cid) for v in futur[:6]])

def stock(hist, cfg):
    """Combien de jours de vidéos programmées il reste."""
    mx = max(1, int(cfg["max_jour"])); now = datetime.now(timezone.utc); auj = datetime.now(TZ).date()
    futur = [v for v in hist if v["pub"] > now]; occ = occupation(hist)
    d, cv = auj + timedelta(days=1), 0
    while len(occ.get(d, [])) >= mx and cv < 60: cv += 1; d += timedelta(days=1)
    j = len(futur) / mx
    return dict(videos=len(futur), jours=int(j) if j == int(j) else round(j, 1), couverts=cv, max=mx,
                demain=len(occ.get(auj + timedelta(days=1), [])),
                dernier=max((v["pub"].astimezone(TZ).date() for v in futur), default=None))

def tel_e164(t):
    t = re.sub(r"\D", "", t or "")
    return ("+33" + t[1:]) if t.startswith("0") else ("+" + t if t else "")

def moyens():
    m = []
    if os.environ.get("NTFY_TOPIC"): m.append("notification push (ntfy)")
    if all(os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "NOTIF_TEL")): m.append("SMS")
    if os.environ.get("NOTIF_EMAIL"): m.append("e-mail")
    return m

def notifier(sujet, texte, cid):
    """Envoie par tous les moyens configurés. Retourne [(moyen, ok, erreur)]."""
    res = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/", headers={"Content-Type": "application/json"},
                data=json.dumps({"topic": topic, "title": sujet, "message": texte, "priority": 4}).encode()), timeout=20)
            res.append(("push", True, ""))
        except Exception as e: res.append(("push", False, str(e)[:120]))
    sid, tok, frm, tel = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "NOTIF_TEL"))
    if sid and tok and frm and tel:
        try:
            rq = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data=urllib.parse.urlencode({"To": tel_e164(tel), "From": frm, "Body": (sujet + "\n" + texte)[:1500]}).encode())
            rq.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
            urllib.request.urlopen(rq, timeout=20); res.append(("SMS", True, ""))
        except Exception as e: res.append(("SMS", False, str(e)[:120]))
    mail = os.environ.get("NOTIF_EMAIL")
    if mail and cid:
        try:
            from email.mime.text import MIMEText
            from email.header import Header
            m = MIMEText(texte, "plain", "utf-8"); m["to"] = mail; m["subject"] = Header(sujet, "utf-8")
            build("gmail", "v1", credentials=creds_for(cid)).users().messages().send(
                userId="me", body={"raw": base64.urlsafe_b64encode(m.as_bytes()).decode()}).execute()
            res.append(("e-mail", True, ""))
        except Exception as e:
            res.append(("e-mail", False, "active Gmail API dans Google Cloud et reconnecte ta chaîne (" + str(e)[:80] + ")"))
    return res

def bilan_texte(cid, nom, yt, hist, cfg):
    an = analytique(cid, yt, 7); t, d = an["tot"], an["delta"]
    fmt = lambda k: f" ({d[k]:+d} %)" if d.get(k) is not None else ""
    now = datetime.now(timezone.utc)
    posts = sum(1 for v in hist if v["pub"] <= now and (now - v["pub"]).days < 7)
    s = stock(hist, cfg)
    L = [f"Bilan de la semaine — {nom}", f"Vues : {t['vues']}{fmt('vues')}", f"Heures vues : {t['heures']}{fmt('heures')}",
         f"Abonnés gagnés : {t['abos']:+d}{fmt('abos')}", f"J'aime : {t['likes']}{fmt('likes')}", f"Vidéos publiées : {posts}"]
    if an["top"]: L.append("Top : " + " | ".join(f"{n if len(n) <= 38 else n[:37] + '…'} ({v} vues)" for n, v, h in an["top"][:3]))
    L.append(f"Stock restant : {s['videos']} vidéo(s), {s['jours']} jour(s)")
    L.append(f"(7 jours jusqu’au {an['fin']}, derniers chiffres confirmés par YouTube, comparés aux 7 jours d’avant.)")
    return "\n".join(L)

def lire_etat():
    return kv_get("notif") or {}

_VERIF = threading.Lock()

def verifier():
    if not _VERIF.acquire(blocking=False): return  # une vérification tourne déjà : on évite les doubles notifications
    try: _verifier()
    finally: _VERIF.release()

def _verifier():
    """Alerte stock bas (8h-22h, au plus toutes les 12 h) + bilan le vendredi soir (dès 18h, une fois)."""
    for cid, nom in liste():
        try: drive_auto(cid, nom)
        except Exception as e: DRIVE_INFO[cid] = "Problème : " + erreur_claire(e)[0]
        try:
            faits = flow_auto(cid, nom)
            if faits:
                CACHE.pop(("h", cid), None); CACHE.pop(("hist", cid), None)
                if moyens(): notifier("Vidéo(s) de Flow programmée(s)", "\n".join(faits), cid)
        except Exception as e: FLOW_INFO[cid] = "Problème : " + erreur_claire(e)[0]
    if not moyens(): return
    now = datetime.now(TZ); etat = lire_etat(); mini = int(os.environ.get("NOTIF_JOURS", "1"))
    for cid, nom in liste():
        try:
            yt = yt_for(cid); cfg = config(cid); hist = historique(yt); s = stock(hist, cfg)
            cle = f"stock:{cid}"
            if 8 <= now.hour < 22 and s["couverts"] < mini and time.time() - etat.get(cle, 0) > 12 * 3600:
                msg = (f"{nom} : plus aucune vidéo programmée. Ajoutes-en sur le site." if not s["videos"] else
                       f"{nom} : stock bas. Demain : {s['demain']}/{s['max']} vidéos. Stock total : {s['videos']} vidéo(s), {s['jours']} jour(s). Ajoutes-en sur le site.")
                if any(ok for _, ok, _ in notifier("Pense à ajouter des vidéos", msg, cid)): etat[cle] = time.time()
            al = int(cfg.get("alerte_vues", 1000) or 0)
            if al > 0 and 8 <= now.hour < 23:  # vidéo publiée il y a 30 min à 24 h, au-dessus du seuil, prévenue une seule fois
                boom = etat.setdefault("boom", {})
                for v in hist:
                    h = (datetime.now(timezone.utc) - v["pub"]).total_seconds() / 3600
                    if v["statut"] == "public" and 0.5 <= h <= 24 and v["vues"] >= al and v["id"] not in boom:
                        t = v["titre"] if len(v["titre"]) <= 50 else v["titre"][:49] + "…"
                        msg = f"« {t} » décolle : {v['vues']} vues en {int(h)} h (ton seuil : {al}). Va voir les stats !"
                        if any(ok for _, ok, _ in notifier("🚀 Une vidéo décolle", msg, cid)): boom[v["id"]] = time.time()
                for k in [k for k, t0 in boom.items() if time.time() - t0 > 3 * 86400]: del boom[k]
            cle = f"bilan:{cid}"
            if now.weekday() == 4 and now.hour >= 18 and etat.get(cle) != str(now.date()):
                if any(ok for _, ok, _ in notifier(f"Bilan de la semaine — {nom}", bilan_texte(cid, nom, yt, hist, cfg), cid)):
                    etat[cle] = str(now.date())
        except Exception: continue
    kv_set("notif", etat)

def boucle():
    time.sleep(60)
    while True:
        try: verifier()
        except Exception: pass
        chauffer()
        time.sleep(600)

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]

def _duree_txt(j):
    if j < 60: return f"{int(round(j))} jours"
    if j < 730: return f"{j / 30.4:.0f} mois"
    return f"{j / 365:.1f} ans".replace(".", ",")

def _quand(j):
    if j > 365 * 80: return "plus de 80 ans"
    d = datetime.now(TZ).date() + timedelta(days=int(j))
    return f"{d.day} {MOIS[d.month - 1]} {d.year} (dans {_duree_txt(j)})"

def prevision(cid, yt, hist):
    """Estimation des vues à venir + date des paliers d'abonnés. Estimation, pas une promesse."""
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    fin = datetime.now(TZ).date()
    rows = ya.reports().query(ids="channel==MINE", startDate=str(fin - timedelta(days=40)), endDate=str(fin),
                              metrics="views,subscribersGained,subscribersLost", dimensions="day", sort="day").execute().get("rows", [])
    if len(rows) >= 4 and rows[-1][1] < 0.5 * sum(r[1] for r in rows[-4:-1]) / 3: rows = rows[:-1]  # dernier jour incomplet
    if len(rows) < 8: raise ValueError("pas assez de jours confirmés par YouTube (il en faut 8)")
    mean = lambda l: sum(l) / len(l)
    v = [r[1] for r in rows]
    r7 = mean(v[-7:]); p7 = mean(v[-14:-7]) if len(v) >= 14 else r7
    g = min(1.03, max(0.97, (r7 / p7) ** (1 / 7))) if p7 > 0 and r7 > 0 else 1.0
    m14 = mean(v[-14:]); cv = statistics.pstdev(v[-14:]) / m14 if m14 > 0 else 0.3
    r = r7
    try:  # on ajoute ce qu'on sait des derniers jours pas encore confirmés
        rc = recent(cid, yt)
        if rc and rc["jours"] >= 1 and rc["est"] > 0:
            r = 0.5 * r7 + 0.5 * min(max(rc["est"] / rc["jours"], 0.5 * r7), 2 * r7)
    except Exception: pass
    now = datetime.now(timezone.utc)
    vp = lambda x: x["vues"] / min((now - x["pub"]).total_seconds() / 86400, 7)
    pub = [x for x in hist if x["statut"] == "public" and (now - x["pub"]).days >= 2]
    sh = [x for x in pub if 0 < x["duree"] <= 180]
    if len(sh) < 6: sh = pub
    gm = mean([vp(x) for x in sh]) if sh else 0
    tl, hl = {}, {}
    if gm > 0 and len(sh) >= 6:
        th, hh = {}, {}
        for x in sh:
            for h in set(hashtags_de(x)): th.setdefault(h, []).append(vp(x))
            hh.setdefault(heure_de(x), []).append(vp(x))
        shrink = lambda l, k: 1 + (mean(l) / gm - 1) * len(l) / (len(l) + k)
        tl = {h: shrink(l, 5) for h, l in th.items() if 3 <= len(l) < len(sh)}
        hl = {h: shrink(l, 3) for h, l in hh.items() if len(l) >= 2}
    fac = lambda x: mean([tl.get(h, 1) for h in hashtags_de(x)] or [1]) * hl.get(heure_de(x), 1)
    rec = [x for x in hist if x["statut"] == "public" and (now - x["pub"]).days < 7]
    up = [x for x in hist if x["pub"] > now]
    f_rec = mean([fac(x) for x in rec]) if rec else 1
    f_rel = min(1.25, max(0.8, mean([fac(x) for x in up]) / f_rec)) if up and f_rec > 0 else 1.0
    span = max(1, (max(x["pub"] for x in up) - now).days + 1) if up else 1
    total = lambda H: sum(r * g ** d * (1 + (f_rel - 1) * min(1, span / d)) for d in range(1, H + 1))
    preds = []
    for lab, H in (("24 heures", 1), ("48 heures", 2), ("1 semaine", 7), ("1 mois", 30)):
        c = total(H); rel = math.sqrt((cv / math.sqrt(H)) ** 2 + 0.15 ** 2 + (0.25 * H / 30) ** 2)
        preds.append((lab, int(c), int(max(0, c * (1 - rel))), int(c * (1 + rel))))
    lifts = sorted(tl.items(), key=lambda x: -x[1])
    net7 = sum(r_[2] - r_[3] for r_ in rows[-7:]); par_jour = net7 / 7
    subs = int(yt.channels().list(part="statistics", mine=True).execute()["items"][0]["statistics"].get("subscriberCount", 0))
    obj = []
    for cible in (20000, 50000, 100000):
        if subs >= cible: obj.append((cible, "déjà atteint", "déjà atteint"))
        elif par_jour <= 0: obj.append((cible, "pas atteignable à ce rythme", "pas atteignable à ce rythme"))
        else:
            j = (cible - subs) / par_jour
            obj.append((cible, _quand(j), _quand(j / 2)))
    return dict(preds=preds, nb=len(sh), r=int(r), tend=f"{(g - 1) * 100:+.1f} %", frel=round(f_rel, 2),
                bons=[h for h, l in lifts if l > 1.05][:3], mauvais=[h for h, l in lifts[::-1] if l < 0.95][:3],
                subs=subs, par_jour=round(par_jour, 1), objectifs=obj)

# ---------- accès ----------
def authed(f):
    def w(*a, **k):
        return f(*a, **k) if session.get("ok") else redirect("/login")
    w.__name__ = f.__name__
    return w

def vue(nom, **kw):
    cid = active()
    fond = (config(cid).get("fond") or "") if cid else ""
    return render_template_string(PAGE, view=nom, chaines=liste(), cid=cid, msg=request.args.get("msg"),
                                  ICONS=ICONS, BADGE=BADGE, fond=fond, **kw)

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    n, t = LOGIN_FAILS.get(ip, (0, 0))
    gn, gt = LOGIN_FAILS.get("*", (0, 0))
    if (n >= 5 and time.time() - t < 600) or (gn >= 25 and time.time() - gt < 600):
        err = "Trop d’essais. Réessaie dans 10 minutes."
    elif request.method == "POST":
        if hmac.compare_digest(request.form.get("pw", "").encode(), PASSWORD.encode()):
            LOGIN_FAILS.pop(ip, None); session.permanent = True; session["ok"] = True
            return redirect("/")
        LOGIN_FAILS[ip] = ((n + 1) if time.time() - t < 600 else 1, time.time())
        LOGIN_FAILS["*"] = ((gn + 1) if time.time() - gt < 600 else 1, time.time())
        err = "Mot de passe incorrect."
    return render_template_string(PAGE, view="login", err=err, chaines=[], cid=None, msg=None)

@app.route("/canal/<cid>")
@authed
def canal(cid):
    session["cid"] = cid
    r = request.args.get("retour", "/")
    return redirect(r if r.startswith("/") and not r.startswith("//") else "/")

def erreur_claire(e):
    """Traduit une erreur Google en phrase simple. Retourne (texte, faut_il_reconnecter)."""
    t = str(e)
    if any(x in t for x in ("invalid_grant", "RefreshError", "expired or revoked", "invalid_client", "unauthorized_client")):
        return "Google a retiré ou fait expirer l'autorisation de cette chaîne.", True
    if "insufficient" in t.lower() and ("scope" in t.lower() or "permission" in t.lower()):
        return "Il manque une autorisation Google : reconnecte la chaîne (bouton « + Chaîne »), puis recommence.", True
    if "quotaExceeded" in t or "rateLimitExceeded" in t: return "Limite quotidienne de YouTube atteinte : ça repart vers 9h.", False
    if "accessNotConfigured" in t or "has not been used" in t: return "Une API Google n'est pas activée dans Google Cloud.", False
    if any(x in t for x in ("timed out", "Connection", "Name or service", "SSL")): return "Google ne répond pas pour l'instant. Réessaie dans un moment.", False
    return t.replace("\n", " ")[:140] or "Erreur inconnue.", False

@app.route("/")
@authed
def home():
    if request.args.get("maj"): CACHE.clear()
    data, erreurs = [], []
    for c, nom in liste():
        try: data.append(cache_sr(("h", c), 60, lambda c=c: donnees_chaine(c)))
        except Exception as e:
            txt, reco = erreur_claire(e); erreurs.append(dict(nom=nom, txt=txt, reco=reco))
    prog = sorted(((f, d["nom"]) for d in data for f in d["futur"]), key=lambda x: x[0]["quand"])
    acc = next((d for d in data if d["cid"] == active()), data[0] if data else None)
    return vue("home", data=data, erreurs=erreurs, prog=prog[:5], nxt=prog[0] if prog else None, act=list(reversed(JOBS[-8:])), acc=acc,
               n_prog=sum(d["prog"] for d in data), n_att=sum(1 for j in JOBS if j["etat"] in ("attente", "envoi")))

@app.route("/publier")
@authed
def publier():
    cid = active(); sem = stk = None
    if cid:
        try:
            d = cache_sr(("h", cid), 60, lambda: donnees_chaine(cid)); sem, stk = d["sem"], d["stock"]
        except Exception: pass
    return vue("publier", c=config(cid) if cid else {"max_jour": 4}, sem=sem, stk=stk)

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
    try: f.fetch_token(authorization_response=BASE + request.full_path)
    except Exception as e: return redirect("/?msg=" + urllib.parse.quote("Connexion impossible : " + str(e)[:120]))
    yt = build("youtube", "v3", credentials=f.credentials)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items: return redirect("/?msg=" + urllib.parse.quote("Aucune chaîne YouTube trouvée sur ce compte Google."))
    ch = items[0]
    d = charger(ch["id"]) or {}
    d.update(nom=ch["snippet"]["title"], token=f.credentials.to_json())
    sauver(ch["id"], d)
    session["cid"] = ch["id"]
    return redirect("/?msg=" + urllib.parse.quote("Chaîne connectée : " + ch["snippet"]["title"]))

@app.route("/upload", methods=["POST"])
@authed
def upload():
    cid = active()
    if not cid: return redirect("/connect")
    jobs, doubles = [], []
    vus = {x["sig"]: x for x in deja_envoyes(cid)}
    en_cours = {j.get("sig") for j in JOBS if j.get("etat") in ("attente", "envoi")}
    for f in request.files.getlist("video"):
        if not f.filename or not (f.mimetype.startswith("video/") or f.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm", ".mkv"))): continue
        p = os.path.join(tempfile.gettempdir(), uuid.uuid4().hex + ".mp4")
        f.save(p)
        if os.path.getsize(p) == 0:
            os.unlink(p); continue
        sig = signature(p)
        if (sig in vus or sig in en_cours) and request.form.get("doublon") != "1":
            doubles.append(f.filename); os.unlink(p); continue
        en_cours.add(sig)
        jobs.append(dict(sig=sig, nom=f.filename, path=p, etat="attente", pct=0, titre="", msg="", warn="", vid="", quand="", ia=""))
    if not jobs:
        if doubles: return redirect("/?msg=" + urllib.parse.quote("Déjà envoyée, ignorée : " + ", ".join(doubles)[:150]))
        return redirect("/?msg=Aucune vidéo reçue : réessaie de choisir tes fichiers.")
    if len(jobs) == 1 and request.form.get("title", "").strip(): jobs[0]["force"] = request.form["title"].strip()
    JOBS[:] = JOBS[-30:] + jobs
    threading.Thread(target=traiter, args=(cid, jobs), daemon=True).start()
    return redirect("/file")

@app.route("/publier-maintenant/<cid>/<vid>", methods=["POST"])
@authed
def publier_maintenant(cid, vid):
    retour = request.form.get("retour", "/")
    if not retour.startswith("/") or retour.startswith("//"): retour = "/"
    def fin(msg): return redirect(retour + ("&" if "?" in retour else "?") + "msg=" + urllib.parse.quote(msg))
    if cid not in [c for c, _ in liste()] or not re.fullmatch(r"[\w-]{6,20}", vid): return fin("Vidéo introuvable.")
    try:
        yt = yt_for(cid)
        it = yt.videos().list(part="status,snippet", id=vid).execute().get("items", [])
        if not it or it[0]["snippet"].get("channelId") != cid: return fin("Vidéo introuvable sur cette chaîne.")
        titre, st = it[0]["snippet"]["title"], it[0]["status"]
        if st["privacyStatus"] == "public": return fin("Cette vidéo est déjà publique.")
        statut = {k: st[k] for k in ("embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids") if k in st}
        statut["privacyStatus"] = "public"  # sans publishAt : la programmation est annulée, la vidéo sort tout de suite
        yt.videos().update(part="status", body={"id": vid, "status": statut}).execute()
        CACHE.pop(("h", cid), None); CACHE.pop(("hist", cid), None)
        return fin("Publiée maintenant : " + titre[:60])
    except Exception as e:
        txt, reco = erreur_claire(e)
        return fin("Impossible de publier : " + txt)

@app.route("/file")
@authed
def file():
    return vue("file")

@app.route("/file/vider", methods=["POST"])
@authed
def file_vider():
    JOBS[:] = [j for j in JOBS if j["etat"] in ("attente", "envoi")]
    return jsonify(ok=True)

@app.route("/file.json")
@authed
def file_json():
    return jsonify([{k: v for k, v in j.items() if k != "path"} for j in JOBS[-12:]])

@app.route("/diagnostic")
@authed
def diagnostic():
    cid = active()
    if not cid: return redirect("/")
    try: seuil = max(1, min(int(request.args.get("seuil", 1500)), 10**6))
    except ValueError: seuil = 1500
    d = perr = pred = perr2 = None
    try:
        yt = yt_for(cid); hist = hist_de(cid)
    except Exception as e:
        hist = None; perr = perr2 = str(e)[:300]
    if hist is not None:
        try: d = analyser(hist, cache(("ret", cid), 300, lambda: retention(cid)), seuil)
        except Exception as e: perr = str(e)[:300]
        try: pred = cache(("prev", cid), 300, lambda: prevision(cid, yt, hist))
        except Exception as e: perr2 = str(e)[:200]
    ia = err = None
    if d and request.args.get("ia"):
        L = lambda r: (f"- «{r['titre']}» | {r['vpj']:.0f} vues/j | {r['duree']} s | rétention {r['ret'] if r['ret'] is not None else '?'} % | "
                       f"{r['jlike']:.1f} j'aime/100 vues | {r['nb_tags']} hashtags | publiée à {r['h']}h")
        p = ("Tu es expert YouTube Shorts ASMR. Voici les vidéos d'une chaîne francophone.\nLES MEILLEURES :\n"
             + "\n".join(L(r) for r in d["top"]) + "\nLES MOINS BONNES :\n" + "\n".join(L(r) for r in d["bas"][:15])
             + "\n\nAnalyse en profondeur pourquoi les moins bonnes n'ont pas percé et donne 8 actions concrètes et précises "
               "(titre, hashtags, durée, accroche des premières secondes, son, miniature, heure, format). Réponds en français, texte simple, sans markdown.")
        ia, err = gemini(p, 2)
    return vue("diag", d=d, seuil=seuil, ia=ia, err=err, perr=perr, pred=pred, perr2=perr2)

@app.route("/calendrier")
@authed
def cal():
    cid = active()
    if not cid: return redirect("/")
    try:
        a, mo = map(int, (request.args.get("m") or datetime.now(TZ).strftime("%Y-%m")).split("-")); datetime(a, mo, 1)
    except Exception:
        a, mo = datetime.now(TZ).year, datetime.now(TZ).month
    m = f"{a}-{mo:02d}"
    jours = {}
    for v in hist_de(cid):
        jours.setdefault(v["pub"].astimezone(TZ).date(), []).append(v)
    sem = calendar.Calendar().monthdatescalendar(a, mo)
    prec = (datetime(a, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    suiv = (datetime(a, mo, 28) + timedelta(days=5)).strftime("%Y-%m")
    now = datetime.now(timezone.utc)
    mois_fr = ["Janvier","Février","Mars","Avril","Mai","Juin","Juillet","Août","Septembre","Octobre","Novembre","Décembre"]
    agenda = {d.isoformat(): [dict(t=v["titre"], h=v["pub"].astimezone(TZ).strftime("%Hh%M"),
              s="Publiée" if v["pub"] < now else "Programmée", v=v["vues"]) for v in l] for d, l in jours.items()}
    return vue("cal", sem=sem, jours=jours, mois=m, mo=mo, prec=prec, suiv=suiv, now=now,
               titre_mois=f"{mois_fr[mo-1]} {a}", today=datetime.now(TZ).date(), agenda=agenda)

@app.route("/bibliotheque")
@authed
def biblio():
    cid = active()
    if not cid: return redirect("/")
    return vue("biblio", vids=hist_de(cid), tz=TZ, now=datetime.now(timezone.utc))

@app.route("/stats")
@authed
def stats():
    cid = active()
    if not cid: return redirect("/")
    j = request.args.get("j", "28"); j = int(j) if j in ("7", "28", "90") else 28
    yt = yt_for(cid); v = mesurees(hist_de(cid))
    top = lambda cles: sorted(((k, n, round(m, 2)) for k, (n, m) in stats_par(v, cles).items()),
                              key=lambda x: -x[2])[:6]
    an, err, mon = None, None, None
    try: an = cache(("an", cid, j), 300, lambda: analytique(cid, yt, j))
    except Exception as e: err = str(e)[:260]
    try: mon = barres(cache(("mon", cid), 600, lambda: monetisation(cid, yt)))
    except Exception: pass
    try: fr = cache(("rec", cid), 300, lambda: recent(cid, yt))
    except Exception: fr = None
    return vue("stats", j=j, an=an, err=err, mon=mon, fr=fr, maj=datetime.now(TZ).strftime("%H:%M"), nm=len(v), mini=MIN_APPRENTISSAGE,
               heures=top(lambda x: [f"{heure_de(x)}h"]), titres=top(lambda x: [x["titre"]]),
               tags=top(hashtags_de))

@app.route("/ia/test", methods=["POST"])
@authed
def ia_test():
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return redirect("/reglages?msg=" + urllib.parse.quote("GEMINI_API_KEY absente sur Render."))
    res = []
    for m in dict.fromkeys([x for x in (os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash") if x] + _modeles_gemini(key)[:3]):
        t0 = time.time()
        try:
            _appel(m, "Réponds seulement : ok", key, 15); res.append(f"{m} : OK en {time.time() - t0:.1f} s")
        except Exception as e: res.append(f"{m} : échec ({str(e)[:60]})")
    return redirect("/reglages?msg=" + urllib.parse.quote(" · ".join(res)))

@app.route("/notif/test", methods=["POST"])
@authed
def notif_test():
    res = notifier("Test ytauto", "Si tu lis ça, les notifications marchent.", active())
    txt = " · ".join(f"{n} : " + ("envoyé" if ok else "échec, " + e) for n, ok, e in res) or "Aucun moyen de notification configuré (voir les variables sur Render)."
    return redirect("/reglages?msg=" + urllib.parse.quote(txt))

@app.route("/notif/bilan", methods=["POST"])
@authed
def notif_bilan():
    cid = active()
    try:
        yt = yt_for(cid); hist = historique(yt); cfg = config(cid)
        nom = next((n for c, n in liste() if c == cid), "")
        res = notifier(f"Bilan de la semaine — {nom}", bilan_texte(cid, nom, yt, hist, cfg), cid)
        txt = " · ".join(f"{n} : " + ("envoyé" if ok else "échec, " + e) for n, ok, e in res) or "Aucun moyen de notification configuré."
    except Exception as e:
        txt = "Impossible de préparer le bilan : " + str(e)[:150]
    return redirect("/reglages?msg=" + urllib.parse.quote(txt))

LEGAL = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t}}</title><style>body{margin:0;background:#0b0b12;color:#f5f5f7;font:17px/1.6 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;padding:24px}
main{max-width:720px;margin:0 auto}h1{font-size:1.8rem}h2{font-size:1.1rem;margin-top:1.6em}a{color:#5ab4ff}p,li{color:#d0d0d8}</style></head>
<body><main>{{body|safe}}<p><a href="/a-propos">À propos</a> · <a href="/confidentialite">Confidentialité</a> · <a href="/conditions">Conditions</a></p></main></body></html>"""

def _contact():
    m = os.environ.get("CONTACT_EMAIL", "")
    return f'Contact : <a href="mailto:{m}">{m}</a>.' if m else "Contact : le propriétaire du site."

@app.route("/a-propos")
def a_propos():
    return render_template_string(LEGAL, t="Programmateur YouTube", body="""<h1>Programmateur YouTube</h1>
<p>Outil personnel qui permet à son propriétaire de programmer ses vidéos YouTube Shorts, de suivre les statistiques de sa propre chaîne
et de recevoir des notifications (stock de vidéos, bilan de la semaine). Il n’est pas destiné au grand public : l’accès est protégé par un mot de passe.</p>
<p>""" + _contact() + "</p>")

@app.route("/confidentialite")
def confidentialite():
    return render_template_string(LEGAL, t="Règles de confidentialité", body="""<h1>Règles de confidentialité</h1>
<h2>Données utilisées</h2><p>Avec l’autorisation du propriétaire de la chaîne, l’application utilise son compte Google pour : envoyer et programmer des vidéos sur YouTube,
lire la liste de ses vidéos et leurs statistiques (YouTube Data API et YouTube Analytics API), et envoyer des e-mails de notification à sa propre adresse (Gmail, envoi uniquement).</p>
<h2>Stockage</h2><p>Les jetons d’accès au compte et les réglages (titres, hashtags, heures de publication) sont enregistrés dans une base de données privée (Upstash)
et sur le serveur d’hébergement (Render), uniquement pour faire fonctionner l’application. Les vidéos envoyées ne sont pas conservées : elles sont transmises à YouTube puis supprimées du serveur.</p>
<h2>Partage</h2><p>Aucune donnée n’est vendue ni partagée avec des tiers. Les titres des vidéos de la chaîne peuvent être envoyés à l’API Gemini de Google pour proposer de nouveaux titres.</p>
<h2>Retirer l’accès</h2><p>L’accès peut être retiré à tout moment sur <a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.
Les données stockées peuvent être effacées sur simple demande.</p>
<p>""" + _contact() + "</p>")

@app.route("/conditions")
def conditions():
    return render_template_string(LEGAL, t="Conditions d’utilisation", body="""<h1>Conditions d’utilisation</h1>
<p>Cette application est un outil personnel, fourni tel quel, sans garantie. Elle agit uniquement au nom de son propriétaire, sur sa propre chaîne YouTube,
dans le respect des conditions d’utilisation de YouTube et des services d’API de Google. Les prévisions et statistiques affichées sont des estimations.</p>
<p>""" + _contact() + "</p>")

@app.route("/cron")
def cron():
    k = os.environ.get("CRON_KEY", "")
    if not k or not hmac.compare_digest(request.args.get("key", ""), k): return "non", 403
    try: verifier()
    except Exception as e: return f"erreur : {e}", 500
    threading.Thread(target=chauffer, daemon=True).start()
    return "ok"

@app.route("/reglages", methods=["GET", "POST"])
@authed
def reglages():
    cid = active()
    if not cid: return redirect("/")
    if request.method == "POST":
        f = request.form
        sp = lambda s: [x.strip() for x in re.split(r"[,\n]", s) if x.strip()]
        d = charger(cid); avant = config(cid).get("flow_auto")
        if f.get("flow_auto") and not avant:  # on (ré)active : on repart d'une base propre
            fl = _flow_ids(cid); fl["base"] = False; kv_set("flow:" + cid, fl)
        d["config"] = {"titres": [x.strip() for x in f["titres"].splitlines() if x.strip()],
                       "oblig": sp(f["oblig"]) or OBLIG_TAGS, "pool": sp(f["pool"]),
                       "heures": [int(x) for x in sp(f["heures"]) if x.isdigit() and int(x) < 24] or DEFAUT_HEURES,
                       "max_jour": max(1, min(10, (int(f["max_jour"]) if f.get("max_jour", "").strip().isdigit() else 4))), "fond": f.get("fond", "").strip(),
                       "apprentissage": bool(f.get("apprentissage")),
                       "alerte_vues": int(f["alerte_vues"]) if f.get("alerte_vues", "").strip().isdigit() else 1000,
                       "flow_auto": bool(f.get("flow_auto")), "drive_auto": bool(f.get("drive_auto")),
                       "drive_dossier": f.get("drive_dossier", "").strip()[:100] or "Flow"}
        sauver(cid, d)
        return redirect("/reglages?msg=Réglages enregistrés.")
    return vue("reglages", c=config(cid), notif=moyens(), stockage=etat_stockage(), flow=FLOW_INFO.get(cid), drive=DRIVE_INFO.get(cid))

PAGE = """{% macro semaine(sem, mx) %}<div class="g wk"><h3>LES 7 PROCHAINS JOURS <em>{{mx}} / jour</em></h3><div class="wd">{% for s in sem %}<div class="dy {{'td' if loop.first}}"><small>{{s.lab}}</small><b>{{s.jour.day}}</b><i class="dots">{% for x in range(mx) %}<u class="{{'f' if x<s.n}}"></u>{% endfor %}</i>{% for h in s.heures %}<span>{{h}}</span>{% endfor %}</div>{% endfor %}</div></div>{% endmacro %}
<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Programmateur YouTube</title><style>:root{--ink:#f5f5f7;--mut:#a3a3ad;--acc:#ff5a36;--blue:#5ab4ff;--pub:rgba(52,199,89,.34);--prog:rgba(255,159,10,.38);--line:rgba(255,255,255,.12);--tint:rgba(255,90,54,.22);--red:#ff5a36}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased;
padding:max(12px,env(safe-area-inset-top)) 16px calc(110px + env(safe-area-inset-bottom));
background:radial-gradient(900px 600px at 12% 8%,rgba(255,90,54,.30),transparent 60%),radial-gradient(900px 650px at 92% 18%,rgba(70,120,255,.32),transparent 60%),radial-gradient(1000px 700px at 55% 105%,rgba(150,60,255,.30),transparent 60%),#08080d fixed;background-attachment:fixed}
body.fond{background:linear-gradient(rgba(0,0,0,.5),rgba(0,0,0,.62)),var(--img) center/cover fixed}
main{max-width:1180px;margin:0 auto}
h1{font-size:1.9rem;font-weight:700;letter-spacing:-.02em;line-height:1.15;margin:.6em 0 .25em}
h2{font-size:1.05rem;font-weight:600;margin:1.6em 0 .55em}h3{margin:0 0 .5em;font-size:.78rem;letter-spacing:.08em;color:var(--mut);font-weight:700}
p{color:var(--mut);margin:.2em 0 1em}a{color:var(--blue);text-decoration:none}
label{display:block;font-weight:600;font-size:.9rem;margin:1.2em 0 .45em}
.g,input,textarea,.v,.msg,table,.cal,.top,.dock,.dock a,.pill,.tile span,.ring{background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(255,255,255,.05));-webkit-backdrop-filter:blur(30px) saturate(1.8);backdrop-filter:blur(30px) saturate(1.8);
border:1px solid rgba(255,255,255,.16);box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 18px 50px rgba(0,0,0,.35)}
.g{border-radius:26px;padding:16px 18px}
input,textarea{width:100%;font:inherit;color:var(--ink);padding:13px 15px;border-radius:16px;outline:0;transition:box-shadow .2s,border-color .2s}
input:focus,textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--tint)}textarea{min-height:90px;resize:vertical}
button,.btn{display:block;width:100%;margin-top:1.4em;padding:15px;font:inherit;font-weight:600;color:#fff;background:linear-gradient(180deg,rgba(255,110,70,.55),rgba(255,70,40,.4));border:1px solid rgba(255,255,255,.25);
box-shadow:inset 0 1px 0 rgba(255,255,255,.3);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border-radius:18px;text-align:center;cursor:pointer;transition:transform .18s cubic-bezier(.2,.8,.2,1),filter .2s}
button.pn{display:inline-block;width:auto;margin-top:8px;padding:7px 14px;font-size:.78rem}button:active,.btn:active,.tile:active,.dock a:active,.pill:active{transform:scale(.95)}button:disabled{opacity:.55}
.top{position:sticky;top:max(8px,env(safe-area-inset-top));z-index:10;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:9px 14px;border-radius:999px;margin-bottom:14px}
.logo{display:flex;width:34px;height:34px;border-radius:11px;background:linear-gradient(180deg,#ff4d3a,#d81f10);align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(255,60,40,.5)}.logo svg{width:16px;height:16px}
.st{font-size:1rem;margin-right:4px}.pills{display:flex;gap:8px;flex-wrap:wrap}.sp{flex:1}
.pill{padding:6px 14px;border-radius:999px;color:var(--ink);font-size:.9rem;display:flex;gap:8px;align-items:center;box-shadow:none;transition:transform .18s}
.pill.on{background:rgba(255,255,255,.2)}.pill em{font-style:normal;font-size:.72rem;background:var(--acc);padding:1px 7px;border-radius:99px;color:#fff;font-weight:700}
.ok{font-size:.82rem;display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:99px;background:rgba(52,199,89,.18)}.ok i{width:8px;height:8px;border-radius:50%;background:#34c759;box-shadow:0 0 8px #34c759}
.tb{font-weight:600;font-variant-numeric:tabular-nums;color:var(--mut);padding:0 6px}
.dock{position:fixed;left:50%;transform:translateX(-50%);bottom:max(14px,env(safe-area-inset-bottom));z-index:20;display:flex;gap:8px;padding:10px;border-radius:28px;max-width:calc(100% - 20px);overflow-x:auto}
.dock a{width:56px;height:56px;border-radius:18px;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--ink);gap:3px;flex:none;box-shadow:none;transition:transform .18s}
.dock a.on{background:rgba(255,255,255,.28);box-shadow:inset 0 1px 0 rgba(255,255,255,.4)}.dock svg,.tile svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.dock span{font-size:.58rem;color:var(--mut)}.dock a.on span{color:var(--ink)}
.hero{display:grid;grid-template-columns:220px 1fr;gap:14px}.clk{padding:14px;display:flex;align-items:center;justify-content:center}.clk svg{width:100%;max-width:190px}
.big{display:grid;grid-template-columns:1.3fr 1fr;gap:18px;padding:20px 24px}.tm{display:flex;align-items:flex-start;line-height:1}.tm b{font-size:5.2rem;font-weight:800;letter-spacing:-.04em;font-variant-numeric:tabular-nums}.tm sup{font-size:1.5rem;color:var(--acc);margin:.5em 0 0 6px;font-weight:700}
.stk2{margin-top:12px;font-size:.98rem;color:#fff;background:rgba(255,255,255,.1);padding:8px 14px;border-radius:14px;display:inline-block;margin-right:6px}.dt{font-weight:700;font-size:1.05rem;margin-top:.3em}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.chips span{font-size:.8rem;padding:6px 12px;border-radius:99px;background:rgba(255,255,255,.12)}
.ap{border-left:1px solid var(--line);padding-left:18px}.ai{display:flex;gap:10px;padding:7px 0;font-size:.88rem;line-height:1.3}.ai b{color:#fff;font-variant-numeric:tabular-nums;border-left:3px solid var(--acc);padding-left:8px;min-width:62px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;margin:18px 0}.tile{display:flex;flex-direction:column;align-items:center;gap:8px;color:var(--ink);font-size:.82rem;font-weight:600;transition:transform .18s}
.tile span{width:68px;height:68px;border-radius:22px;display:flex;align-items:center;justify-content:center}.tile svg{width:28px;height:28px}
.chs{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}.cch{display:flex;align-items:center;gap:12px}.cch img{width:46px;height:46px;border-radius:50%;border:2px solid var(--acc)}.cch>div:nth-child(2){flex:1}.cch b{font-size:1.05rem}
.rg{width:54px;height:54px}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}.mini .g{padding:10px 14px;border-radius:18px}.mini span{display:block;font-size:.62rem;letter-spacing:.07em;color:var(--mut);font-weight:700}.mini b{font-size:1.5rem}.mini small{color:var(--mut);font-size:.85rem}
.wid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:14px}
.nx{display:flex;align-items:center;gap:16px}.nr{position:relative;width:110px;height:110px;flex:none}.nr svg{width:110px;height:110px}.nr div{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:1.35rem;font-weight:800;font-variant-numeric:tabular-nums}.nr small{font-size:.6rem;color:var(--mut);font-weight:500}
.wk{margin-top:14px}.wk h3 em{float:right;font-style:normal;background:rgba(255,255,255,.12);padding:2px 9px;border-radius:99px;letter-spacing:0}.wd{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}
.dy{background:rgba(255,255,255,.07);border-radius:18px;padding:10px 4px;text-align:center;font-size:.7rem;min-height:120px}.dy small{display:block;color:var(--mut);font-weight:700;letter-spacing:.05em}.dy b{display:block;font-size:1.25rem}.dy.td{background:linear-gradient(180deg,rgba(255,90,54,.55),rgba(255,90,54,.25))}.dy.td small{color:#fff}
.dots{display:flex;justify-content:center;gap:3px;margin:5px 0}.dots u{width:5px;height:5px;border-radius:50%;background:rgba(255,255,255,.2)}.dots u.f{background:#fff}.dy span{display:block;color:var(--mut);font-size:.66rem}.dy.td span{color:#fff}
.drop{display:flex;flex-direction:column;align-items:center;gap:8px;padding:34px 20px;text-align:center;cursor:pointer;border:1.5px dashed rgba(255,255,255,.3);border-radius:26px;background:rgba(255,255,255,.05);margin:0}.drop.ov{background:var(--tint)}
.drop svg{width:54px;height:54px;padding:14px;border-radius:18px;background:rgba(255,255,255,.15);fill:none;stroke:#fff;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.drop b{font-size:1.25rem}.drop span{color:var(--mut);font-size:.88rem}.drop em{font-style:normal;color:var(--acc);font-weight:700}
.msg{padding:13px 16px;border-radius:18px;margin:1em 0}.err{color:#ff7a66}
.bar{display:none;height:8px;border-radius:99px;overflow:hidden;margin-top:1em;background:var(--line)}.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .25s}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}.v{border-radius:20px;overflow:hidden;font-size:.88rem}.v img{width:100%;display:block}.v div{padding:10px 12px}.mut{color:var(--mut)}
table{width:100%;border-collapse:separate;border-spacing:0;border-radius:20px;overflow:hidden;font-size:.9rem}td,th{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line)}
.ch{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.ch>div{display:flex;gap:6px}.ch a{padding:7px 14px;border-radius:999px;background:rgba(255,255,255,.1);color:var(--ink);font-weight:600;font-size:.9rem}.ch a.on{background:var(--tint);color:#fff}
.cal{border-radius:26px;overflow:hidden;margin-top:.6em}.cw,.cg{display:grid;grid-template-columns:repeat(7,1fr)}.cw span{text-align:center;padding:9px 0;font-size:.78rem;color:var(--mut);border-bottom:1px solid var(--line)}
.d{min-height:104px;padding:5px 4px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);cursor:pointer;overflow:hidden;transition:background .2s}.d:nth-child(7n){border-right:0}.d:active,.d.sel{background:rgba(255,255,255,.12)}.d.out{opacity:.35}
.n{display:inline-flex;width:26px;height:26px;align-items:center;justify-content:center;border-radius:50%;font-size:.9rem;margin-bottom:2px}.today .n{background:var(--red);color:#fff;font-weight:700}
.e{display:block;font-style:normal;font-size:.68rem;line-height:1.25;padding:2px 5px;margin-top:2px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.e.pub{background:var(--pub)}.e.prog{background:var(--prog)}.more{display:block;font-style:normal;font-size:.68rem;color:var(--mut);padding:1px 5px}
.ag{margin-top:14px}.ag h3{margin:6px 0}.it{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line);font-size:.92rem}.dot{width:4px;border-radius:4px;flex:none}.dot.pub{background:#34c759}.dot.prog{background:#ff9f0a}
.chk{display:flex;gap:12px;align-items:center;font-weight:500}.chk input{width:24px;height:24px;padding:0}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:.8em 0}.kp{margin:0;text-align:left;font-weight:400;color:var(--ink);padding:14px 16px;background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.05));border-radius:22px}.kp.sel{box-shadow:inset 0 0 0 1.5px var(--blue),0 18px 50px rgba(0,0,0,.35)}
.kp span{display:block;font-size:.8rem;color:var(--mut)}.kp b{font-size:1.8rem;letter-spacing:-.02em;display:block}.kp em{font-style:normal;font-size:.8rem;color:var(--mut)}.kp em.up{color:#34c759}.kp em.dn{color:#ff6b5a}
.chart{position:relative;padding:14px 14px 8px;border-radius:26px}.chart svg{width:100%;display:block;touch-action:pan-y}.tt{position:absolute;pointer-events:none;background:rgba(20,20,28,.92);border:1px solid var(--line);border-radius:12px;padding:6px 10px;font-size:.8rem;display:none;white-space:nowrap;transform:translate(-50%,-110%)}
.box{border-radius:24px;padding:10px 16px}.row{position:relative;display:flex;justify-content:space-between;padding:9px 0;font-size:.9rem}.row i{position:absolute;left:0;bottom:2px;height:3px;border-radius:3px;background:var(--blue);opacity:.8}.row em{font-style:normal;color:var(--mut)}
.pb{margin:12px 0}.pt{display:flex;justify-content:space-between;font-size:.9rem;margin-bottom:6px}.pt b{font-weight:600}.tr{height:10px;border-radius:99px;background:var(--line);overflow:hidden}.tr i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--acc),#ffa05a);transition:width .4s}
.job{padding:12px 16px;border-radius:20px;margin:10px 0}.job ul{margin:.4em 0 0;padding-left:1.1em;font-size:.88rem}.job li{margin:.25em 0}
@media(max-width:820px){.hero{grid-template-columns:1fr}.big{grid-template-columns:1fr}.ap{border-left:0;padding-left:0}.tm b{font-size:4rem}.d{min-height:74px}.e{font-size:.6rem}.wd{gap:4px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body class="{{'fond' if fond}}" style="{{'--img:url('~fond~')' if fond}}"><main>
{% if view=='login' %}
<h1>Accès privé</h1><form method="post"><label>Mot de passe</label>
<input type="password" name="pw" autofocus><button>Entrer</button>{% if err %}<p class="err">{{err}}</p>{% endif %}</form>
{% else %}
<header class="top"><a class="logo" href="/"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="#fff"/></svg></a><b class="st">Studio</b>
<div class="pills">{% for i,n in chaines %}<a class="pill {{'on' if i==cid}}" href="/canal/{{i}}?retour={{request.path}}">{{n}}{% if BADGE.get(i) %}<em>{{BADGE[i]}}</em>{% endif %}</a>{% endfor %}<a class="pill" href="/connect">+ Chaîne</a></div>
<a class="pill" href="?maj=1">↻ Actualiser</a><span class="sp"></span><span class="ok"><i></i>Connecté</span><span id="tb" class="tb"></span></header>
<nav class="dock">{% for k,l,u,i in [('home','Accueil','/','home'),('publier','Publier','/publier','upload'),('stats','Stats','/stats','chart'),('cal','Agenda','/calendrier','cal'),('biblio','Vidéos','/bibliotheque','grid'),('diag','Analyse','/diagnostic','search'),('file','File','/file','list'),('reglages','Réglages','/reglages','sliders')] %}<a class="{{'on' if view==k}}" href="{{u}}"><svg viewBox="0 0 24 24">{{ICONS[i]|safe}}</svg><span>{{l}}</span></a>{% endfor %}</nav>
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
{% if not cid %}<h1>Connecte ta chaîne YouTube</h1><a class="btn" href="/connect">Connecter ma chaîne</a>
{% elif view=='home' %}
{% for e in erreurs %}<div class="msg err">⚠️ <b>{{e.nom}}</b> : {{e.txt}}{% if e.reco %} <a href="/connect">Reconnecter la chaîne</a>{% endif %}</div>{% endfor %}
{% if not data and not erreurs %}<div class="msg">Données YouTube indisponibles pour l’instant. Réessaie dans un instant.</div>{% endif %}
<div class="hero"><div class="g clk"><svg id="ck" viewBox="0 0 200 200"><circle cx="100" cy="100" r="97" fill="rgba(0,0,0,.28)" stroke="rgba(255,255,255,.22)" stroke-width="2"/>
<line id="hh" x1="100" y1="100" x2="100" y2="58" stroke="#fff" stroke-width="5" stroke-linecap="round"/><line id="mm" x1="100" y1="100" x2="100" y2="32" stroke="#fff" stroke-width="3.5" stroke-linecap="round"/>
<line id="ss" x1="100" y1="112" x2="100" y2="22" stroke="#ff5a36" stroke-width="1.8" stroke-linecap="round"/><circle cx="100" cy="100" r="4.5" fill="#ff5a36"/></svg></div>
<div class="g big"><div><span id="gr" class="mut"></span><div class="tm"><b id="hm">--:--</b><sup id="sc"></sup></div><div class="dt" id="dt"></div>
{% for d in data %}<div class="stk2"><b>{{d.nom}}</b> · {{d.stock.videos}} vidéo(s) en stock{% if d.stock.videos %} · de quoi tenir <b>{{d.stock.jours}} jour(s)</b> (jusqu’au {{d.stock.dernier.strftime('%d/%m')}}){% else %} · plus rien de programmé !{% endif %}</div>{% endfor %}
<div class="chips"><span>{{n_prog}} programmée(s)</span><span>{{n_att}} en attente</span><span>{{chaines|length}} chaîne(s)</span></div></div>
<div class="ap"><h3>AU PROGRAMME</h3>{% for f,nom in prog %}<div class="ai"><b>{{f.quand.strftime('%H:%M')}}</b><div>{{f.titre[:36]}}<br><span class="mut">{{nom}} · {{f.jour}}</span>
<form method="post" action="/publier-maintenant/{{f.cid}}/{{f.id}}" onsubmit="return confirm('Publier cette vidéo tout de suite ?')"><input type="hidden" name="retour" value="/"><button class="pn">Publier maintenant</button></form></div></div>{% else %}<p>Rien de programmé pour l’instant.</p>{% endfor %}</div></div></div>
<div class="tiles">{% for u,l,i in [('/publier','Publier','upload'),('/stats','Analytique','chart'),('/calendrier','Calendrier','cal'),('/bibliotheque','Bibliothèque','grid'),('/diagnostic','Diagnostic','search'),('/reglages','Réglages','sliders')] %}
<a class="tile" href="{{u}}"><span><svg viewBox="0 0 24 24">{{ICONS[i]|safe}}</svg></span>{{l}}</a>{% endfor %}</div>
<div class="chs">{% for d in data %}<div class="g"><div class="cch"><img src="{{d.img}}" alt=""><div><b>{{d.nom}}</b><br><span class="mut">{{d.abos}} abonnés · {{d.vues|k}} vues au total</span></div>
<svg class="rg" viewBox="0 0 54 54"><circle cx="27" cy="27" r="22" fill="none" stroke="rgba(255,255,255,.15)" stroke-width="5"/><circle cx="27" cy="27" r="22" fill="none" stroke="#ff5a36" stroke-width="5" stroke-linecap="round" stroke-dasharray="138.2" stroke-dashoffset="{{(138.2*(1-([d.aujourd/d.max,1]|min)))|round(1)}}" transform="rotate(-90 27 27)"/><text x="27" y="32" text-anchor="middle" fill="#fff" font-size="15" font-weight="700">{{d.aujourd}}</text></svg></div>
<div class="mini"><div class="g"><span>AUJOURD’HUI</span><b>{{d.aujourd}}<small>/{{d.max}}</small></b></div><div class="g"><span>PROGRAMMÉES</span><b>{{d.prog}}</b></div><div class="g"><span>VUES 28 J</span><b>{{d.vues28|k if d.vues28 is not none else '–'}}</b></div></div></div>{% endfor %}</div>
<div class="wid"><div class="g"><h3>STOCK DE VIDÉOS</h3>{% for d in data %}<div class="pb"><div class="pt"><span>{{d.nom}}</span><b>{{d.stock.videos}} vidéos</b></div><div class="tr"><i style="width:{{[d.stock.jours/7*100,100]|min}}%"></i></div>
<small class="mut">{% if d.stock.videos %}De quoi tenir {{d.stock.jours}} jour(s) · jusqu’au {{d.stock.dernier.strftime('%d/%m')}}{% else %}Plus aucune vidéo programmée : ajoutes-en !{% endif %}</small></div>{% endfor %}</div>
<div class="g"><h3>PROCHAINE PUBLICATION</h3>{% if nxt %}<div class="nx"><div class="nr" id="nr" data-iso="{{nxt[0].iso}}"><svg viewBox="0 0 110 110"><circle cx="55" cy="55" r="48" fill="none" stroke="rgba(255,255,255,.14)" stroke-width="8"/><circle id="nrc" cx="55" cy="55" r="48" fill="none" stroke="#ff5a36" stroke-width="8" stroke-linecap="round" stroke-dasharray="301.6" stroke-dashoffset="301.6" transform="rotate(-90 55 55)"/></svg><div><span id="nt">--:--</span><small>h:min</small></div></div>
<div><b>{{nxt[0].titre[:40]}}</b><br><span class="mut">{{nxt[1]}}<br>{{nxt[0].jour}} à {{nxt[0].quand.strftime('%H:%M')}}</span></div></div>{% else %}<p>Aucune vidéo programmée.</p>{% endif %}</div>
<div class="g"><h3>ACTIVITÉ</h3>{% for j in act %}<div class="it"><span class="dot {{'pub' if j.etat=='ok' else 'prog'}}"></span><div>{{(j.titre or j.nom)[:42]}}<br><span class="mut">{{ {'ok':'Programmée','erreur':'Erreur','envoi':'Envoi en cours','attente':'En attente'}[j.etat] }}{% if j.quand %} · {{j.quand}}{% endif %}</span></div></div>{% else %}<p>Aucune activité récente.</p>{% endfor %}</div>
<div class="g"><h3>OBJECTIFS</h3>{% if acc %}<div class="pb"><div class="pt"><span>Monétisation : abonnés</span><b>{{acc.abos}} / 1000</b></div><div class="tr"><i style="width:{{[acc.abos/10,100]|min}}%"></i></div></div><p style="font-size:.8rem"><a href="/stats">Voir toute la progression</a></p>{% endif %}</div></div>
{% if acc %}{{semaine(acc.sem,acc.max)}}{% endif %}
{% elif view=='publier' %}
<h1>Dépose tes vidéos.</h1><p>Tu peux en choisir plusieurs. Maximum {{c.max_jour}} par jour, publiées 5 min avant les heures de pointe. Le jour suivant se remplit tout seul.</p>
{% if stk %}<div class="g stk"><b>Stock : {{stk.videos}} vidéo(s)</b> · {% if stk.videos %}de quoi tenir {{stk.jours}} jour(s), jusqu’au {{stk.dernier.strftime('%d/%m')}}{% else %}plus rien de programmé : ajoutes-en !{% endif %}</div>{% endif %}
<form id="fm" method="post" action="/upload" enctype="multipart/form-data">
<label class="drop" id="dz" for="vf"><svg viewBox="0 0 24 24">{{ICONS['upload']|safe}}</svg><b>Dépose tes vidéos ici</b><span>ou touche pour les choisir : titre, hashtags et horaire sont choisis tout seuls</span><em id="nf"></em></label>
<input id="vf" name="video" type="file" accept="video/*" multiple hidden>
<label>Titre (facultatif, seulement si tu envoies une seule vidéo)</label><input name="title">
<div class="bar" id="bar"><i></i></div><p id="wt"></p><button id="bt">Programmer</button></form>
<script>
vf.addEventListener('change',function(){nf.textContent=vf.files.length?vf.files.length+' vidéo(s) choisie(s)':''});dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('ov')});dz.addEventListener('dragleave',function(){dz.classList.remove('ov')});dz.addEventListener('drop',function(e){e.preventDefault();dz.classList.remove('ov');vf.files=e.dataTransfer.files;nf.textContent=vf.files.length+' vidéo(s) choisie(s)'});
fm.addEventListener('submit',function(e){e.preventDefault();if(!vf.files.length){wt.textContent='Choisis au moins une vidéo.';return}var x=new XMLHttpRequest();x.open('POST','/upload');
x.upload.onprogress=function(v){if(v.lengthComputable){var p=Math.round(v.loaded/v.total*100);bar.firstChild.style.width=p+'%';wt.textContent=p<100?'Envoi '+p+' %':'Réception terminée…'}};
x.onload=function(){if(x.status>=400){wt.textContent='Erreur côté site, réessaie.';bt.disabled=false}else{location.href=x.responseURL||'/'}};
x.onerror=function(){wt.textContent='Erreur réseau, réessaie.';bt.disabled=false};
bar.style.display='block';bt.disabled=true;x.send(new FormData(fm))});
</script>
{% if sem %}{{semaine(sem,c.max_jour)}}{% endif %}
{% elif view=='cal' %}
<div class="ch"><h1>{{titre_mois}}</h1><div><a href="/calendrier?m={{prec}}">‹</a><a href="/calendrier">Aujourd’hui</a><a href="/calendrier?m={{suiv}}">›</a></div></div>
<div class="cal"><div class="cw">{% for j in ['lun.','mar.','mer.','jeu.','ven.','sam.','dim.'] %}<span>{{j}}</span>{% endfor %}</div>
<div class="cg">{% for s in sem %}{% for d in s %}<div class="d {{'out' if d.month!=mo}} {{'today' if d==today}}" data-d="{{d.isoformat()}}">
<span class="n">{{d.day}}</span>{% for v in jours.get(d,[])[:3] %}<i class="e {{'pub' if v.pub<now else 'prog'}}">{{v.titre}}</i>{% endfor %}
{% if jours.get(d,[])|length>3 %}<i class="more">+{{jours.get(d)|length-3}} de plus</i>{% endif %}</div>{% endfor %}{% endfor %}</div></div>
<div class="ag g" id="ag"></div>
<script>
var A={{agenda|tojson}};var cells=document.querySelectorAll('.d');
function show(c){cells.forEach(function(x){x.classList.remove('sel')});c.classList.add('sel');var k=c.dataset.d,l=A[k]||[],h='<h3>'+k.split('-').reverse().join('/')+'</h3>';
if(!l.length)h+='<p>Rien ce jour-là.</p>';l.forEach(function(v){var t=document.createElement('div');t.textContent=v.t;
h+='<div class="it"><span class="dot '+(v.s=='Publiée'?'pub':'prog')+'"></span><div><b>'+t.innerHTML+'</b><br><span class="mut">'+v.s+' · '+v.h+' · '+v.v+' vues</span></div></div>'});
document.getElementById('ag').innerHTML=h}
cells.forEach(function(c){c.addEventListener('click',function(){show(c)})});
var t=document.querySelector('.today')||document.querySelector('.d:not(.out)');if(t)show(t);
</script>
{% elif view=='biblio' %}
<h1>Bibliothèque</h1><div class="grid">{% for v in vids %}<div class="v"><img src="{{v.img}}" alt=""><div><b>{{v.titre}}</b><br>
<span class="mut">{{v.vues}} vues · {{v.likes}} j’aime<br>{{'Programmée' if v.pub>now else v.statut}} · {{v.pub.astimezone(tz).strftime('%d/%m %Hh')}}</span>
{% if v.statut!='public' %}<form method="post" action="/publier-maintenant/{{cid}}/{{v.id}}" onsubmit="return confirm('Publier cette vidéo tout de suite ?')"><input type="hidden" name="retour" value="/bibliotheque"><button class="pn">Publier maintenant</button></form>{% endif %}</div></div>{% endfor %}</div>
{% elif view=='stats' %}
<div class="ch"><h1>Stats</h1><div>{% for d in [7,28,90] %}<a class="{{'on' if j==d}}" href="/stats?j={{d}}">{{d}} j</a>{% endfor %}</div></div>
{% if fr %}<div class="msg">Dernier jour confirmé par YouTube : <b>{{fr.dernier}}</b>{% if fr.jours>0 %} ({{fr.jours}} j de retard côté YouTube){% endif %}. Depuis, environ <b>+{{fr.est|k}} vues</b> en direct (estimation). Actualisé à {{maj}}.</div>{% else %}<p>Actualisé à {{maj}}. YouTube confirme ses chiffres détaillés avec 2 à 3 jours de retard.</p>{% endif %}
{% if err %}<div class="msg">Les stats YouTube détaillées ne sont pas encore autorisées.<br>
1) Dans Google Cloud, active « YouTube Analytics API ». 2) Reconnecte ta chaîne avec le bouton ci-dessous.
<a class="btn" href="/connect">Reconnecter ma chaîne</a><p style="font-size:.75rem">{{err}}</p></div>
{% elif an %}
<div class="kpi">{% for k,t,vl in [('vues','Vues',an.tot.vues),('heures','Heures de visionnage',an.tot.heures),('abos','Abonnés',an.tot.abos),('likes','J’aime',an.tot.likes)] %}
<button type="button" class="g kp {{'sel' if k=='vues'}}" data-k="{{k}}"><span>{{t}}</span><b>{{vl|k}}</b>{% set dl=an.delta[k] %}<em class="{{'up' if dl and dl>0 else 'dn' if dl and dl<0}}">{% if dl is none %}—{% else %}{{'+' if dl>0}}{{dl}} % vs période précédente{% endif %}</em></button>{% endfor %}</div>
<div class="g chart" id="ch"></div>
<h2>Comment les gens te trouvent</h2><div class="g box">{% for nom,vl in an.src %}<div class="row"><span>{{nom}}</span><i style="width:{{(vl/an.src[0][1]*100)|round}}%"></i><em>{{vl}}</em></div>{% endfor %}</div>
<h2>Contenu le plus regardé</h2><table>{% for t,vl,h in an.top %}<tr><td>{{loop.index}}</td><td>{{t}}</td><td>{{vl}} vues</td><td>{{h}} h</td></tr>{% endfor %}</table>
<script>
var AN={{an|tojson}},NM={vues:'vues',heures:'h',abos:'abonnés',likes:'j’aime'};
function nice(m){if(m<=0)return 1;var e=Math.pow(10,Math.floor(Math.log(m)/Math.LN10)),f=m/e;return(f<=1?1:f<=2?2:f<=5?5:10)*e}
function fk(x){var a=Math.abs(x);return a>=1e6?(x/1e6).toFixed(1).replace('.0','')+' M':a>=1000?(x/1000).toFixed(1).replace('.0','')+' k':(Math.round(x*10)/10)+''}
function draw(k){var el=document.getElementById('ch'),v=AN[k],L=AN.labels,n=v.length;if(!n){el.textContent='Pas encore de données.';return}
var W=900,H=300,pl=48,pr=14,pt=14,pb=30,hi=nice(Math.max.apply(null,v)),lo=Math.min(0,Math.min.apply(null,v));if(lo<0)lo=-nice(-lo);
var X=function(i){return pl+i*(W-pl-pr)/Math.max(n-1,1)},Y=function(x){return pt+(1-(x-lo)/(hi-lo))*(H-pt-pb)};
var g='',i;for(i=0;i<=4;i++){var gv=lo+(hi-lo)*i/4,gy=Y(gv);g+='<line x1="'+pl+'" x2="'+(W-pr)+'" y1="'+gy+'" y2="'+gy+'" stroke="rgba(255,255,255,.1)"/><text x="'+(pl-8)+'" y="'+(gy+4)+'" text-anchor="end" fill="#a3a3ad" font-size="12">'+fk(gv)+'</text>'}
for(i=0;i<5;i++){var xi=Math.round(i*(n-1)/4);g+='<text x="'+X(xi)+'" y="'+(H-8)+'" text-anchor="middle" fill="#a3a3ad" font-size="12">'+L[xi]+'</text>'}
var d='M'+X(0)+' '+Y(v[0]);for(i=1;i<n;i++){var mx=(X(i-1)+X(i))/2;d+=' C'+mx+' '+Y(v[i-1])+' '+mx+' '+Y(v[i])+' '+X(i)+' '+Y(v[i])}
el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'"><defs><linearGradient id="gr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5ab4ff" stop-opacity=".45"/><stop offset="1" stop-color="#5ab4ff" stop-opacity="0"/></linearGradient></defs>'+g+'<path d="'+d+' L'+X(n-1)+' '+Y(lo)+' L'+X(0)+' '+Y(lo)+'Z" fill="url(#gr)"/><path d="'+d+'" fill="none" stroke="#5ab4ff" stroke-width="3" stroke-linecap="round"/><line id="cl" y1="'+pt+'" y2="'+(H-pb)+'" stroke="rgba(255,255,255,.4)" stroke-dasharray="4" style="display:none"/><circle id="cd" r="6" fill="#5ab4ff" stroke="#fff" stroke-width="2" style="display:none"/></svg><div class="tt" id="tt"></div>';
var svg=el.querySelector('svg'),tt=document.getElementById('tt'),cl=document.getElementById('cl'),cd=document.getElementById('cd');
function mv(e){var r=svg.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*W,j=Math.max(0,Math.min(n-1,Math.round((x-pl)/((W-pl-pr)/Math.max(n-1,1)))));
cl.setAttribute('x1',X(j));cl.setAttribute('x2',X(j));cl.style.display='';cd.setAttribute('cx',X(j));cd.setAttribute('cy',Y(v[j]));cd.style.display='';
tt.style.display='block';tt.style.left=(svg.offsetLeft+X(j)/W*r.width)+'px';tt.style.top=(svg.offsetTop+Y(v[j])/H*r.height)+'px';tt.innerHTML='<b>'+L[j]+'</b> · '+fk(v[j])+' '+NM[k]}
svg.addEventListener('pointermove',mv);svg.addEventListener('pointerdown',mv)}
document.querySelectorAll('.kp').forEach(function(b){b.addEventListener('click',function(){document.querySelectorAll('.kp').forEach(function(x){x.classList.remove('sel')});b.classList.add('sel');draw(b.dataset.k)})});
draw('vues');
</script>
{% endif %}
<h2>Ce que le site a appris</h2><p>{{nm}} vidéos mesurées sur {{mini}} nécessaires. {{'Actif : titres, hashtags et heures s’adaptent à ce qui marche.' if nm>=mini else 'Phase de test : il essaie des variantes pour voir ce qui marche.'}}</p>
{% for nom,l in [('Meilleures heures',heures),('Meilleurs titres',titres),('Meilleurs hashtags',tags)] %}<h2>{{nom}}</h2>
<table><tr><th>Valeur</th><th>Vidéos</th><th>Score</th></tr>{% for k,n2,m in l %}<tr><td>{{k}}</td><td>{{n2}}</td><td>{{m}}</td></tr>{% endfor %}</table>{% endfor %}
{% if mon %}<h2>Progression vers la monétisation</h2><div class="g box">
{% for n,v,c,p in mon %}<div class="pb"><div class="pt"><span>{{n}}</span><b>{{v}} / {{c}} · {{p}} %</b></div><div class="tr"><i style="width:{{p}}%"></i></div></div>{% endfor %}
<p style="font-size:.8rem">Voie Shorts : 1 000 abonnés + 10 M de vues Shorts sur 90 jours. À partir du 1er février 2027, les nouveaux inscrits devront 20 M.</p></div>{% endif %}
{% elif view=='file' %}
<div class="ch"><h1>File d’attente</h1><div><a href="#" id="vd">Vider la liste</a></div></div><p>Garde cette page ouverte pendant l’envoi. « Vider la liste » enlève seulement les lignes terminées, jamais les vidéos sur YouTube.</p><div id="q"></div>
<script>
function esc(t){var d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML}
function load(){fetch('/file.json').then(function(r){return r.json()}).then(function(L){
document.getElementById('q').innerHTML=L.map(function(j){var s={attente:'En attente',envoi:'Envoi '+j.pct+' %',ok:'Programmée',erreur:'Erreur'}[j.etat];
return '<div class="g job"><b>'+esc(j.titre||j.nom)+'</b><div class="mut">'+s+(j.quand?' · '+esc(j.quand):'')+(j.vid?' · <a href="https://studio.youtube.com/video/'+j.vid+'/edit" target="_blank">Ouvrir dans Studio</a>':'')+'</div>'
+(j.etat=='envoi'?'<div class="tr"><i style="width:'+j.pct+'%"></i></div>':'')+(j.warn?'<p class="err">'+esc(j.warn)+'</p>':'')+(j.msg?'<p class="err">'+esc(j.msg)+'</p>':'')+(j.ia?'<p class="mut">IA indisponible : '+esc(j.ia)+'</p>':'')+'</div>'}).join('')||'<p>Rien en cours.</p>'})}
document.getElementById('vd').addEventListener('click',function(e){e.preventDefault();fetch('/file/vider',{method:'POST'}).then(load)});
load();setInterval(load,3000);
</script>
{% elif view=='diag' %}
<div class="ch"><h1>Diagnostic</h1><div>{% for s in [500,1500,5000] %}<a class="{{'on' if seuil==s}}" href="/diagnostic?seuil={{s}}">&lt; {{s}}/j</a>{% endfor %}</div></div>
{% if pred %}<h2>Prévision de vues</h2>
<div class="kpi">{% for lab,c,lo,hi in pred.preds %}<div class="g kp"><span>{{lab}}</span><b>{{c|k}}</b><em>entre {{lo|k}} et {{hi|k}}</em></div>{% endfor %}</div>
<div class="g box"><p style="font-size:.88rem">Basé sur tes <b>{{pred.nb}} Shorts récents</b> : rythme actuel ≈ <b>{{pred.r|k}} vues par jour</b> ({{pred.tend}} par jour de tendance). Les hashtags et les heures des vidéos déjà programmées changent la prévision de <b>×{{pred.frel}}</b> par rapport à tes dernières publications.{% if pred.bons %} Hashtags qui rapportent chez toi : {{pred.bons|join(' ')}}.{% endif %}{% if pred.mauvais %} À éviter : {{pred.mauvais|join(' ')}}.{% endif %} C’est une estimation à partir de tes propres données, pas une garantie : un Short qui décolle change tout.</p></div>
<h2>Projection d’abonnés</h2>
<p>Tu as environ <b>{{pred.subs}}</b> abonnés et tu en gagnes <b>{{pred.par_jour}}</b> par jour en moyenne sur les 7 derniers jours confirmés.</p>
<table><tr><th>Objectif</th><th>À ce rythme</th><th>Si tu doubles le rythme</th></tr>{% for c,a,b in pred.objectifs %}<tr><td>{{c|k}} abonnés</td><td>{{a}}</td><td>{{b}}</td></tr>{% endfor %}</table>
{% elif perr2 %}<div class="msg">Prévision indisponible : {{perr2}}</div>{% endif %}
<h2>Pourquoi certaines vidéos n’ont pas percé</h2>
<p>Vidéos publiées depuis plus de 2 jours, comparées à tes meilleures. Seuil : {{seuil}} vues par jour.</p>
{% if perr %}<div class="msg">Impossible d’analyser pour l’instant : {{perr}}</div>
{% elif not d %}<div class="msg">Pas assez de vidéos publiées depuis plus de 2 jours pour analyser (il en faut au moins 4).</div>{% else %}
<p>Vues par jour = vues ÷ min(âge, 7 jours) : on regarde la première semaine, quand un Short fait l’essentiel de ses vues.</p>
{% if not d.ref_seuil %}<div class="msg">Moins de 3 vidéos dépassent {{seuil}} vues/jour : la comparaison se fait avec tes {{d.nb_ref}} meilleures vidéos, qui ne sont pas listées ci-dessous.</div>{% endif %}
<div class="kpi"><div class="g"><span>Sous le seuil</span><b>{{d.bas|length}}</b></div><div class="g"><span>Au-dessus</span><b>{{d.nb_bons}}</b></div><div class="g"><span>Analysées</span><b>{{d.n}}</b></div></div>
{% for c in d.conseils %}<div class="msg">{{c}}</div>{% endfor %}
<h2>Meilleures vidéos contre les autres</h2>
<table><tr><th></th><th>Meilleures</th><th>Sous le seuil</th></tr>{% for l,b,m in d.comp %}<tr><td>{{l}}</td><td>{{b}}</td><td>{{m}}</td></tr>{% endfor %}</table>
{% if d.tags_bons %}<h2>Hashtags : vues par jour</h2><table><tr><th>Hashtag</th><th>Vidéos</th><th>Vues/jour</th></tr>
{% for h,n,v in d.tags_bons %}<tr><td>{{h}}</td><td>{{n}}</td><td>{{v}}</td></tr>{% endfor %}{% for h,n,v in d.tags_mauv %}<tr><td>{{h}} (faible)</td><td>{{n}}</td><td>{{v}}</td></tr>{% endfor %}</table>{% endif %}
{% if d.heures %}<h2>Heures : vues par jour</h2><table>{% for h,n,v in d.heures[:6] %}<tr><td>{{h}}h</td><td>{{n}} vidéos</td><td>{{v}}</td></tr>{% endfor %}</table>{% endif %}
<h2>Vidéo par vidéo</h2>
{% for r in d.bas %}<div class="g job"><b>{{r.titre}}</b><div class="mut">{{r.vpj|round|int}} vues/jour · {{r.vues}} vues · {{r.duree}} s{% if r.ret is not none %} · rétention {{r.ret|round|int}} %{% endif %}</div>
<ul>{% for x in r.why %}<li>{{x}}</li>{% endfor %}</ul></div>{% endfor %}
<h2>Analyse approfondie par l’IA</h2>
{% if ia %}<div class="g box" style="white-space:pre-wrap">{{ia}}</div>
{% else %}{% if err %}<div class="msg">IA indisponible : {{err}}</div>{% endif %}<a class="btn" href="/diagnostic?seuil={{seuil}}&ia=1">Lancer l’analyse IA</a>{% endif %}
{% endif %}
{% elif view=='reglages' %}
<h1>Réglages</h1><p>Stockage des données : {{stockage}}</p><form method="post">
<label>Tes titres (un par ligne)</label><textarea name="titres">{{c.titres|join('\n')}}</textarea>
<label>Hashtags obligatoires (toujours en description)</label><input name="oblig" value="{{c.oblig|join(', ')}}">
<label>Hashtags candidats en plus (le site choisit les meilleurs parmi eux, 15 par vidéo)</label><textarea name="pool">{{c.pool|join(', ')}}</textarea>
<label>Heures de pointe (YouTube Studio, onglet Audience). Publication 5 min avant.</label><input name="heures" value="{{c.heures|join(', ')}}">
<label>Vidéos maximum par jour</label><input name="max_jour" type="number" min="1" max="10" value="{{c.max_jour}}">
<label>Alerte « ça décolle » : vues en 24 h pour être prévenu (0 = pas d'alerte)</label><input name="alerte_vues" type="number" min="0" value="{{c.alerte_vues}}">
<label>Image de fond (lien, facultatif)</label><input name="fond" value="{{c.fond}}" placeholder="https://…">
<label class="chk"><input type="checkbox" name="apprentissage" {{'checked' if c.apprentissage}}> Apprendre de mes résultats</label>
<label class="chk"><input type="checkbox" name="drive_auto" {{'checked' if c.drive_auto}}> Publier automatiquement les vidéos que je dépose dans mon dossier Google Drive</label>
<label>Nom du dossier Google Drive</label><input name="drive_dossier" value="{{c.drive_dossier}}" placeholder="Flow">{% if drive %}<p class="mut" style="font-size:.85rem">{{drive}}</p>{% endif %}
<label class="chk"><input type="checkbox" name="flow_auto" {{'checked' if c.flow_auto}}> Programmer automatiquement les vidéos envoyées depuis Google Flow (en privé, de moins de 24 h)</label>{% if flow %}<p class="mut" style="font-size:.85rem">{{flow}}</p>{% endif %}
<button>Enregistrer</button></form>
<h2>Notifications</h2><div class="g box"><p>Moyens actifs : {{notif|join(', ') if notif else 'aucun (voir les variables sur Render)'}}.</p>
<p style="font-size:.85rem">Alerte quand le stock est bas, et bilan de la semaine chaque vendredi dès 18h.</p>
<form method="post" action="/notif/test"><button type="submit">Envoyer une notification de test</button></form>
<form method="post" action="/ia/test"><button type="submit">Tester l’IA (Gemini)</button></form>
<form method="post" action="/notif/bilan"><button type="submit">Envoyer le bilan de la semaine maintenant</button></form></div>
{% endif %}{% endif %}
</main>{% if view!='login' %}<script>
(function(){var $=function(i){return document.getElementById(i)};function p2(n){return(n<10?'0':'')+n}
function tick(){var d=new Date(),h=d.getHours();if($('tb'))$('tb').textContent=p2(h)+':'+p2(d.getMinutes());
if($('hm')){$('hm').textContent=p2(h)+':'+p2(d.getMinutes());$('sc').textContent=p2(d.getSeconds());$('gr').textContent=h<5?'Bonne nuit':h<12?'Bonjour':h<18?'Bon après-midi':'Bonsoir';
var s=d.toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long',year:'numeric'});$('dt').textContent=s.charAt(0).toUpperCase()+s.slice(1)}
if($('ck')){var sc=d.getSeconds(),m=d.getMinutes()+sc/60,hh=h%12+m/60;$('hh').setAttribute('transform','rotate('+hh*30+' 100 100)');$('mm').setAttribute('transform','rotate('+m*6+' 100 100)');$('ss').setAttribute('transform','rotate('+sc*6+' 100 100)')}
var nr=$('nr');if(nr){var rem=new Date(nr.dataset.iso)-d;if(rem<0)rem=0;var mn=Math.floor(rem/60000);$('nt').textContent=p2(Math.floor(mn/60))+':'+p2(mn%60);$('nrc').setAttribute('stroke-dashoffset',Math.min(rem,864e5)/864e5*301.6)}}
if($('ck')){var t='';for(var i=0;i<60;i++){var a=i*6*Math.PI/180,l=i%5?4:9;t+='<line x1="'+(100+Math.sin(a)*(90-l))+'" y1="'+(100-Math.cos(a)*(90-l))+'" x2="'+(100+Math.sin(a)*90)+'" y2="'+(100-Math.cos(a)*90)+'" stroke="rgba(255,255,255,'+(i%5?.35:.9)+')" stroke-width="'+(i%5?1:2.4)+'"/>'}$('ck').insertAdjacentHTML('afterbegin',t)}
tick();setInterval(tick,1000)})();
</script>{% endif %}</body></html>"""

threading.Thread(target=boucle, daemon=True).start()
