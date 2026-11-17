# YatriPulse – Real-time Bus Tracking (India)

Flask + Flask-SocketIO + SQLite + Leaflet/OpenStreetMap.

## Run (VS Code terminal)
    python -m venv venv && venv\Scripts\activate     # Windows  (Linux/Mac: source venv/bin/activate)
    pip install -r requirements.txt
    python app.py
Open http://localhost:5000

## Panels
| Panel | URL | Login |
|---|---|---|
| Passenger | `/` | none – search routes, watch buses live |
| Driver | `/driver` | driver1 … driverN / driver123 (one per seeded bus) |
| Admin | `/admin` | admin / admin123 |

## How it works
- Admin clicks the map to place stops anywhere in India, names the route, saves -> pushed over WebSocket, the route appears on every open passenger map instantly.
- Admin adds buses + driver accounts per route.
- Driver presses *Start trip*; browser GPS is streamed via Socket.IO; passengers see the bus move live. (GPS needs HTTPS on phones when deployed; localhost works.)
- Buses with no live driver are moved by a built-in simulator (set `SIMULATE=0` to disable).

## Dataset
`data/routes.csv`, `data/stops.csv` – 9 routes across Bengaluru, Delhi, Mumbai, Chennai, Hyderabad, Kolkata, Pune, Jaipur. Add rows and delete `yatripulse.db` to re-seed.

## Before production
Change `SECRET_KEY` and the default passwords; serve with gunicorn+eventlet behind HTTPS.
