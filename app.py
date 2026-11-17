import csv, io, json, math, os, sqlite3, threading, time, datetime
from flask import Flask, render_template, request, redirect, session, jsonify, Response
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE, "yatripulse.db"))
SIMULATE = os.environ.get("SIMULATE", "1") == "1"   # moves buses that have no live driver

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
LIVE = {}   # bus_id -> {lat,lng,speed,ts,live}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        return
    c = db()
    c.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw TEXT, role TEXT, bus_id INTEGER);
    CREATE TABLE routes(id INTEGER PRIMARY KEY, name TEXT, city TEXT, stops TEXT);
    CREATE TABLE buses(id INTEGER PRIMARY KEY, number TEXT UNIQUE, route_id INTEGER);
    """)
    stops = {}
    for r in csv.DictReader(open(os.path.join(BASE, "data/stops.csv"))):
        stops.setdefault(int(r["route_id"]), []).append(
            {"name": r["stop_name"], "lat": float(r["lat"]), "lng": float(r["lng"])})
    for r in csv.DictReader(open(os.path.join(BASE, "data/routes.csv"))):
        rid = int(r["route_id"])
        c.execute("INSERT INTO routes VALUES(?,?,?,?)", (rid, r["name"], r["city"], json.dumps(stops[rid])))
        for k in (1, 2):
            bid = c.execute("INSERT INTO buses(number,route_id) VALUES(?,?)",
                            (f"{r['name'].split()[0]}-B{k}", rid)).lastrowid
            c.execute("INSERT INTO users(username,pw,role,bus_id) VALUES(?,?,?,?)",
                      (f"driver{bid}", generate_password_hash("driver123"), "driver", bid))
    c.execute("INSERT INTO users(username,pw,role) VALUES('admin',?,'admin')", (generate_password_hash("admin123"),))
    c.commit()


def routes_json():
    return [{"id": r["id"], "name": r["name"], "city": r["city"], "stops": json.loads(r["stops"])}
            for r in db().execute("SELECT * FROM routes")]


def buses_json():
    out = []
    for b in db().execute("SELECT * FROM buses"):
        p = LIVE.get(b["id"], {})
        out.append({"id": b["id"], "number": b["number"], "route_id": b["route_id"], **p})
    return out


def need(role):
    return session.get("role") == role


# ---------- pages ----------
@app.route("/")
def passenger():
    return render_template("passenger.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?", (request.form["username"],)).fetchone()
        if u and u["active"] != 0 and check_password_hash(u["pw"], request.form["password"]):
            session.update(role=u["role"], user=u["username"], bus_id=u["bus_id"])
            return redirect("/admin" if u["role"] == "admin" else "/driver")
        err = "Invalid username or password"
    return render_template("login.html", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/admin")
def admin():
    return render_template("admin.html") if need("admin") else redirect("/login")


@app.route("/driver")
def driver():
    if not need("driver"):
        return redirect("/login")
    b = db().execute("SELECT b.*, r.name rname FROM buses b JOIN routes r ON r.id=b.route_id WHERE b.id=?",
                     (session["bus_id"],)).fetchone()
    return render_template("driver.html", bus=b)


# ---------- API ----------
@app.route("/api/routes")
def api_routes():
    return jsonify(routes_json())


@app.route("/api/buses")
def api_buses():
    return jsonify(buses_json())


@app.route("/api/admin/routes", methods=["POST"])
def add_route():
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    d = request.get_json()
    if len(d.get("stops", [])) < 2 or not d.get("name"):
        return jsonify(error="Need a name and at least 2 stops"), 400
    c = db()
    rid = c.execute("INSERT INTO routes(name,city,stops) VALUES(?,?,?)",
                    (d["name"], d.get("city", ""), json.dumps(d["stops"]))).lastrowid
    c.commit()
    r = next(x for x in routes_json() if x["id"] == rid)
    socketio.emit("route_added", r)          # appears instantly on every passenger map
    return jsonify(r)


@app.route("/api/admin/buses", methods=["POST"])
def add_bus():
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    d = request.get_json()
    c = db()
    try:
        bid = c.execute("INSERT INTO buses(number,route_id) VALUES(?,?)", (d["number"], d["route_id"])).lastrowid
        c.execute("INSERT INTO users(username,pw,role,bus_id) VALUES(?,?,?,?)",
                  (d["driver_username"], generate_password_hash(d["driver_password"]), "driver", bid))
        c.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="Bus number or driver username already exists"), 400
    socketio.emit("bus_added", next(b for b in buses_json() if b["id"] == bid))
    return jsonify(ok=True)


@app.route("/api/admin/routes/<int:rid>", methods=["DELETE"])
def del_route(rid):
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    c = db()
    for b in c.execute("SELECT id FROM buses WHERE route_id=?", (rid,)).fetchall():
        c.execute("DELETE FROM users WHERE bus_id=?", (b["id"],))
        LIVE.pop(b["id"], None)
    c.execute("DELETE FROM buses WHERE route_id=?", (rid,))
    c.execute("DELETE FROM routes WHERE id=?", (rid,))
    c.commit()
    socketio.emit("route_removed", {"id": rid})
    return jsonify(ok=True)



def migrate():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS points(id INTEGER PRIMARY KEY, bus_id INT, lat REAL, lng REAL, speed REAL, ts REAL)")
    try:
        c.execute("ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    c.commit()


def off_route_m(lat, lng, st):
    """Distance in metres from a point to the route polyline."""
    k, cl, best = 111320, math.cos(math.radians(lat)), 1e12
    for a, b in zip(st, st[1:]):
        ax, ay = (a["lng"] - lng) * k * cl, (a["lat"] - lat) * k
        bx, by = (b["lng"] - lng) * k * cl, (b["lat"] - lat) * k
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


@app.route("/api/admin/buses")
def admin_buses():
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    rows = db().execute("SELECT b.id,b.number,r.name rname,u.username,u.active FROM buses b "
                        "JOIN routes r ON r.id=b.route_id LEFT JOIN users u ON u.bus_id=b.id").fetchall()
    return jsonify([{"id": x["id"], "number": x["number"], "route": x["rname"], "driver": x["username"],
                     "active": x["active"] != 0, "off": LIVE.get(x["id"], {}).get("off", False),
                     "status": LIVE.get(x["id"], {}).get("status", "")} for x in rows])


@app.route("/api/admin/stats")
def admin_stats():
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    now, c = time.time(), db()
    total = c.execute("SELECT COUNT(*) FROM buses").fetchone()[0]
    live = sum(1 for v in LIVE.values() if v.get("live") and now - v.get("ts", 0) < 30)
    today = time.mktime(datetime.date.today().timetuple())
    pts = c.execute("SELECT COUNT(*) FROM points WHERE ts>=?", (today,)).fetchone()[0]
    return jsonify(total=total, live=live, points_today=pts)


@app.route("/api/admin/buses/<int:bid>/password", methods=["POST"])
def reset_pw(bid):
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    pw = (request.get_json() or {}).get("password", "")
    if len(pw) < 6:
        return jsonify(error="Password must be at least 6 characters"), 400
    c = db()
    c.execute("UPDATE users SET pw=? WHERE bus_id=?", (generate_password_hash(pw), bid))
    c.commit()
    return jsonify(ok=True)


@app.route("/api/admin/buses/<int:bid>/toggle", methods=["POST"])
def toggle_driver(bid):
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    c = db()
    c.execute("UPDATE users SET active = CASE WHEN active=0 THEN 1 ELSE 0 END WHERE bus_id=?", (bid,))
    c.commit()
    return jsonify(ok=True)


@app.route("/api/admin/routes/<int:rid>", methods=["PUT"])
def rename_route(rid):
    if not need("admin"):
        return jsonify(error="forbidden"), 403
    name = (request.get_json() or {}).get("name", "").strip()
    if not name:
        return jsonify(error="Name required"), 400
    c = db()
    c.execute("UPDATE routes SET name=? WHERE id=?", (name, rid))
    c.commit()
    socketio.emit("route_added", next(x for x in routes_json() if x["id"] == rid))
    return jsonify(ok=True)


@app.route("/api/admin/history.csv")
def history_csv():
    if not need("admin"):
        return "forbidden", 403
    bid, hrs = request.args.get("bus_id", type=int), request.args.get("hours", 24, type=int)
    q, a = "SELECT p.*, b.number FROM points p JOIN buses b ON b.id=p.bus_id WHERE p.ts>=?", [time.time() - hrs * 3600]
    if bid:
        q, a = q + " AND p.bus_id=?", a + [bid]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["bus", "time", "lat", "lng", "speed_kmh"])
    for r in db().execute(q + " ORDER BY p.ts", a):
        w.writerow([r["number"], datetime.datetime.fromtimestamp(r["ts"]).isoformat(), r["lat"], r["lng"], round(r["speed"], 1)])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=trip_history.csv"})


# ---------- real-time ----------
SAVED = {}
STATUSES = {"On time", "Delayed", "Bus full", "Breakdown"}


@socketio.on("driver_location")
def driver_location(d):
    if session.get("role") != "driver":
        return
    bid, ts = session["bus_id"], d.get("ts") or time.time()
    c = db()
    r = c.execute("SELECT r.stops FROM routes r JOIN buses b ON b.route_id=r.id WHERE b.id=?", (bid,)).fetchone()
    off = bool(r and off_route_m(d["lat"], d["lng"], json.loads(r["stops"])) > 400)   # geofence: 400 m
    if ts - SAVED.get(bid, 0) >= 5:                                                    # save a point every 5 s
        SAVED[bid] = ts
        c.execute("INSERT INTO points(bus_id,lat,lng,speed,ts) VALUES(?,?,?,?,?)", (bid, d["lat"], d["lng"], d.get("speed") or 0, ts))
        c.commit()
    LIVE[bid] = {"lat": d["lat"], "lng": d["lng"], "speed": d.get("speed") or 0, "ts": ts, "live": True,
                 "off": off, "status": LIVE.get(bid, {}).get("status", "On time")}
    socketio.emit("bus_update", {"id": bid, **LIVE[bid]})


@socketio.on("driver_status")
def driver_status(d):
    if session.get("role") != "driver" or d.get("status") not in STATUSES:
        return
    bid = session["bus_id"]
    LIVE.setdefault(bid, {})["status"] = d["status"]
    if "lat" in LIVE[bid]:
        socketio.emit("bus_update", {"id": bid, **LIVE[bid]})


def simulator():
    """Demo mode: buses without a live driver glide along their route so the map is never empty."""
    prog = {}
    while True:
        time.sleep(2)
        if not SIMULATE:
            continue
        routes = {r["id"]: r["stops"] for r in routes_json()}
        for b in buses_json():
            if b.get("live") and time.time() - b.get("ts", 0) < 15:
                continue
            st = routes.get(b["route_id"])
            if not st:
                continue
            n = len(st) - 1
            t = prog.get(b["id"], (b["id"] * 0.37) % 1.0 * n) + 0.04
            prog[b["id"]] = t if t < n * 2 else 0
            u = prog[b["id"]]
            u = u if u <= n else 2 * n - u            # ping-pong along the route
            i = min(int(u), n - 1)
            f = u - i
            lat = st[i]["lat"] + (st[i + 1]["lat"] - st[i]["lat"]) * f
            lng = st[i]["lng"] + (st[i + 1]["lng"] - st[i]["lng"]) * f
            LIVE[b["id"]] = {"lat": lat, "lng": lng, "speed": 28, "ts": time.time(), "live": False}
            socketio.emit("bus_update", {"id": b["id"], **LIVE[b["id"]]})


init_db()
migrate()
threading.Thread(target=simulator, daemon=True).start()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), allow_unsafe_werkzeug=True)
