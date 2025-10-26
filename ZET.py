import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from google.transit import gtfs_realtime_pb2
from typing import Optional, Dict, List, Any

TZ = ZoneInfo("Europe/Zagreb")
HERE = os.path.abspath(os.path.dirname(__file__))
GTFS_RT_URL = "https://www.zet.hr/gtfs-rt-protobuf"
VOZNIPARK_FILE = os.path.join(HERE, "voznipark.txt")

def safe_input(prompt=""):
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        print("\nIzlaz.")
        sys.exit(0)

def divider(title=None):
    print("\n" + "="*70)
    if title:
        print(f"  {title}")
        print("="*70)

def read_gtfs(name):
    path = os.path.join(HERE, f"{name}.txt")
    if not os.path.exists(path):
        return None
    try:
        return pd.read_csv(path, dtype=str, low_memory=False)
    except Exception as e:
        return None

def load_static():
    files = ["agency","routes","trips","stop_times","stops","calendar","calendar_dates","feed_info","shapes"]
    dfs = {f: read_gtfs(f) for f in files}
    return dfs

def epoch_to_local(sec):
    try:
        return datetime.fromtimestamp(int(sec), tz=timezone.utc).astimezone(TZ)
    except Exception:
        return None

def parse_realtime():
    try:
        resp = requests.get(GTFS_RT_URL, timeout=15)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        return feed
    except Exception as e:
        raise RuntimeError(f"Greška pri dohvaćanju GTFS-RT: {e}")

def find_route_matches(routes_df, q):
    if routes_df is None:
        return None
    q = str(q).strip().lower()
    mask = pd.Series([False]*len(routes_df))
    if "route_short_name" in routes_df.columns:
        mask |= routes_df["route_short_name"].fillna("").str.lower().str.contains(q, na=False)
    if "route_id" in routes_df.columns:
        mask |= routes_df["route_id"].fillna("").str.lower().str.contains(q, na=False)
    return routes_df[mask]

def active_services(calendar, calendar_dates, target_date):
    active = set()
    weekdays = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    target_weekday = target_date.weekday()

    if calendar is not None:
        calendar = calendar.copy()
        calendar["start_date_dt"] = pd.to_datetime(calendar["start_date"], format="%Y%m%d", errors='coerce').dt.date
        calendar["end_date_dt"] = pd.to_datetime(calendar["end_date"], format="%Y%m%d", errors='coerce').dt.date
        
        valid_date = (calendar["start_date_dt"].fillna(date(1900,1,1)) <= target_date) & \
                     (calendar["end_date_dt"].fillna(date(2900,1,1)) >= target_date)
        valid_weekday = calendar[weekdays[target_weekday]].astype(str) == "1"
        
        active.update(calendar[valid_date & valid_weekday]["service_id"].dropna().astype(str).tolist())
        
    if calendar_dates is not None:
        calendar_dates = calendar_dates.copy()
        calendar_dates["date_dt"] = pd.to_datetime(calendar_dates["date"], format="%Y%m%d", errors='coerce').dt.date
        exceptions = calendar_dates[calendar_dates["date_dt"] == target_date]
        
        active.update(exceptions[exceptions["exception_type"].astype(str) == "1"]["service_id"].dropna().astype(str).tolist())
        active.difference_update(exceptions[exceptions["exception_type"].astype(str) == "2"]["service_id"].dropna().astype(str).tolist())
        
    return active

def get_static_trips_for_route(trips, stop_times, stops, route_id, active):
    out = []
    if trips is None or stop_times is None:
        return out
    cand = trips[trips["route_id"] == route_id].copy()
    if "service_id" in cand.columns and active:
        cand = cand[cand["service_id"].isin(active)]
    
    stop_names = {}
    if stops is not None and "stop_id" in stops.columns and "stop_name" in stops.columns:
        stop_names = stops.set_index("stop_id")["stop_name"].to_dict()

    for _, t in cand.iterrows():
        trip_id = t["trip_id"]
        sts = stop_times[stop_times["trip_id"] == trip_id].sort_values("stop_sequence")
        stops_list = []
        for _, s in sts.iterrows():
            stop_id = s.get("stop_id")
            name = stop_names.get(stop_id, stop_id)
            stops_list.append({"stop_id": stop_id, "stop_name": name, "arrival": s.get("arrival_time"), "departure": s.get("departure_time"), "seq": s.get("stop_sequence")})
        out.append({"trip_id": trip_id, "headsign": t.get("trip_headsign"), "stops": stops_list})
    return out

def correlate(feed, trips, route_id=None, vehicle_id=None, stop_id=None):
    matches = []
    trip_to_route = trips.set_index('trip_id')['route_id'].to_dict() if trips is not None and "trip_id" in trips.columns and "route_id" in trips.columns else {}

    for entity in feed.entity:
        tu = entity.trip_update if entity.HasField("trip_update") else None
        veh = entity.vehicle if entity.HasField("vehicle") else None
        
        trip = None
        if tu is not None and tu.HasField("trip"):
            trip = tu.trip
        elif veh is not None and veh.HasField("trip"):
            trip = veh.trip
            
        trip_id = getattr(trip, "trip_id", None)
        matched = False
        
        if route_id:
            if trip_id in trip_to_route and trip_to_route[trip_id] == route_id:
                matched = True
        elif vehicle_id and veh is not None:
            vid = getattr(getattr(veh, "vehicle", None), "id", None)
            if vid and str(vid).lower() == str(vehicle_id).lower():
                matched = True
        elif stop_id and tu is not None:
            for stu in tu.stop_time_update:
                if getattr(stu, "stop_id", None) == stop_id:
                    matched = True
                    break
                    
        if matched:
            matches.append({"entity_id": getattr(entity, "id", None), "trip_id": trip_id, "trip_update": tu, "vehicle": veh})
            
    return matches

def humanize_trip(tu, stops_df):
    if tu is None:
        return []
    lines = []
    try:
        trip = tu.trip
        trip_id = getattr(trip, "trip_id", "-")
        route_id = getattr(trip, "route_id", "-")
        head = getattr(trip, "trip_headsign", None) or "-"
        lines.append(f"🚌 Trip ID: {trip_id} | Linija: {route_id} | Smjer: {head}")
        
        stop_names = {}
        if stops_df is not None and "stop_id" in stops_df.columns and "stop_name" in stops_df.columns:
             stop_names = stops_df.set_index("stop_id")["stop_name"].to_dict()
             
        for stu in tu.stop_time_update:
            stop_id = getattr(stu, "stop_id", "-")
            stop_name = stop_names.get(stop_id, stop_id)
            seq = getattr(stu, "stop_sequence", "-")
            
            arr = getattr(stu, "arrival", None)
            dep = getattr(stu, "departure", None)
            
            arr_t = epoch_to_local(arr.time) if arr is not None and getattr(arr, "time", None) else None
            dep_t = epoch_to_local(dep.time) if dep is not None and getattr(dep, "time", None) else None
            
            arr_s = arr_t.strftime("%H:%M") if arr_t else "-"
            dep_s = dep_t.strftime("%H:%M") if dep_t else "-"
            
            delay = getattr(arr, "delay", None) if arr is not None else None
            delay_s = f"{delay}s" if delay is not None else "-"
            
            lines.append(f"    • {stop_name} ({stop_id}) | seq {seq} | dolazak {arr_s} | odlazak {dep_s} | kašnjenje {delay_s}")
            
    except Exception as e:
        lines.append(f"(Ne mogu pročitati trip_update: {e})")
    return lines

def humanize_vehicle(veh, voznipark):
    if veh is None:
        return []
    lines = []
    
    vid = getattr(getattr(veh, "vehicle", None), "id", None)
    
    model_info = next((v for v in voznipark if v["garazni"] == str(vid)), None)
    model_s = f"Model: {model_info['model']}" if model_info and model_info.get("model") else ""
    
    try:
        trip = getattr(veh, "trip", None)
        if trip and hasattr(trip, "trip_id"):
            lines.append(f"🔗 trip_id: {trip.trip_id} | route: {getattr(trip,'route_id','-')}")
        
        if vid:
            lines.append(f"🚍 Vehicle ID: {vid} {model_s}")
            
        if getattr(veh, "position", None):
            pos = veh.position
            lat = getattr(pos, "latitude", None)
            lon = getattr(pos, "longitude", None)
            speed = getattr(pos, "speed", None)
            current_stop_id = getattr(veh, "stop_id", None)
            
            lat_s = f"{lat:.5f}" if isinstance(lat, float) else str(lat)
            lon_s = f"{lon:.5f}" if isinstance(lon, float) else str(lon)
            
            lines.append(f"📍 Lokacija: {lat_s},{lon_s} | Brzina: {speed or '-'} | Stop ID: {current_stop_id or '-'}")
            
        if getattr(veh, "current_stop_sequence", None) is not None:
            lines.append(f"➡ Trenutni stop seq: {veh.current_stop_sequence}")
            
        if getattr(veh, "timestamp", None):
            ts = epoch_to_local(veh.timestamp)
            if ts:
                lines.append(f"🕒 Ažurirano: {ts.strftime('%Y-%m-%d %H:%M:%S')}")
                
    except Exception as e:
        lines.append(f"(Ne mogu pročitati vehicle: {e})")
    return lines

def load_voznipark():
    if not os.path.exists(VOZNIPARK_FILE):
        return []
    out = []
    try:
        with open(VOZNIPARK_FILE, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("/")
                if len(parts) >= 3:
                    garazni = parts[0].strip()
                    reg = parts[1].strip()
                    model = "/".join(parts[2:]).strip()
                    out.append({"garazni": garazni, "reg": reg, "model": model, "raw": s})
                else:
                    out.append({"garazni": None, "reg": None, "model": s, "raw": s})
    except Exception:
        return []
    return out

def find_stops_by_name(stops_df, name_query):
    if stops_df is None or "stop_name" not in stops_df.columns:
        return []
    q = str(name_query).strip().lower()
    mask = stops_df["stop_name"].fillna("").str.lower().str.contains(q, na=False)
    results = stops_df[mask].copy()
    if results.empty:
        return []
    if "stop_id" in results.columns:
        return results[["stop_id","stop_name"]].drop_duplicates().to_dict("records")
    return []

def time_str_to_seconds(t):
    try:
        parts = str(t).split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = int(parts[0]), int(parts[1]), 0
        else:
            return None
        return h*3600 + m*60 + s
    except Exception:
        return None

def main_menu():
    
    while True:
        try:
            divider("ZETpy - preglednik")
            print("Molimo pričekajte...")
            dfs = load_static()
            routes = dfs.get("routes")
            trips = dfs.get("trips")
            stop_times = dfs.get("stop_times")
            stops = dfs.get("stops")
            calendar = dfs.get("calendar")
            calendar_dates = dfs.get("calendar_dates")
            voznipark = load_voznipark()
            
            divider("Izbornik")
            print("1) Pretraga po liniji")
            print("2) Pretraga po vozilu (garažni broj ili registracija)")
            print("3) Pretraga po ID stanice")
            print("4) Pretraga po nazivu stanice")
            print("5) Statistika voznog parka")
            print("6) Izlaz")
            
            choice_str = safe_input("Unesite broj opcije: ").strip()
            
            if choice_str == '6':
                print("Kraj.")
                safe_input("Pritisnite Enter za izlaz...")
                return

            try:
                choice = int(choice_str)
            except ValueError:
                print("Nevažeći unos.")
                continue

            if choice not in range(1, 6):
                print("Nepoznata opcija.")
                continue

            print("\nIzaberi izvor podataka:")
            print("1) Realtime (trenutni GTFS-RT)")
            print("2) Statika za određeni datum/vrijeme (GTFS datoteke)")
            print("3) Oba")
            mode = safe_input("Unesite 1/2/3: ").strip()
            
            if mode not in ("1","2","3"):
                print("Nevažeći unos.")
                continue

            user_dt = None
            if mode in ("2","3"):
                d = safe_input("Datum (YYYY-MM-DD, enter za danas): ").strip()
                t = safe_input("Vrijeme (HH:MM, enter za 00:00): ").strip()
                
                if not d:
                    d = date.today().isoformat()
                
                try:
                    if d and t:
                        user_dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                    elif d and not t:
                        user_dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ)
                    elif t and not d:
                        today = date.today()
                        user_dt = datetime.strptime(f"{today.isoformat()} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                except Exception:
                    print("Neispravan datum/vrijeme.")
                    continue
            
            if choice == 1:
                search_by_route(routes, trips, stop_times, stops, calendar, calendar_dates, mode, user_dt, voznipark)
            elif choice == 2:
                search_by_vehicle(trips, stops, mode, voznipark)
            elif choice == 3:
                search_by_stop_id(trips, stop_times, stops, calendar, calendar_dates, mode, user_dt)
            elif choice == 4:
                search_by_stop_name(trips, stop_times, stops, calendar, calendar_dates, mode, user_dt)
            elif choice == 5:
                show_voznipark_stats(voznipark)
            else:
                print("Nepoznata opcija.")
                
            reset = safe_input("\nPritisnite Enter za nastavak ili unesite 'R' za povratak na izbornik: ").strip().upper()
            if reset != 'R':
                return # Kraj programa ako nije 'R'

        except Exception as e:
            print("Neočekivana pogreška:", e)
            safe_input("Pritisnite Enter za povratak na izbornik...")
            continue

def search_by_route(routes, trips, stop_times, stops, calendar, calendar_dates, mode, user_dt, voznipark):
    q = safe_input("Unesite broj linije ili route_id (npr. 109): ").strip()
    matched = find_route_matches(routes, q)
    if matched is None or len(matched) == 0:
        print("Linija nije pronađena u routes.txt.")
        return
        
    if len(matched) > 1:
        for i, r in matched.reset_index(drop=True).iterrows():
            print(f"[{i}] {r['route_id']} {r.get('route_short_name')} {r.get('route_long_name')}")
        sel = safe_input("Odaberite indeks (enter za 0): ").strip()
        try:
            idx = int(sel) if sel else 0
        except Exception:
            idx = 0
        if idx >= len(matched):
            print("Nevažeći indeks.")
            return
        route_row = matched.reset_index(drop=True).iloc[idx]
    else:
        route_row = matched.iloc[0]
        
    route_id = route_row["route_id"]
    divider(f"Linija {route_row.get('route_short_name') or route_id}")
    
    if mode in ("2","3"):
        if user_dt:
            active = active_services(calendar, calendar_dates, user_dt.date())
            stat_trips = get_static_trips_for_route(trips, stop_times, stops, route_id, active)
            print(f"Staticki tripovi ({user_dt.date().isoformat()}): {len(stat_trips)}")
            for t in stat_trips[:10]:
                print(f" trip {t['trip_id']} smjer {t.get('headsign') or '-'} stops {len(t['stops'])}")
        else:
            print("Ne mogu prikazati statiku jer datum/vrijeme nije definirano.")
            
    if mode in ("1","3"):
        try:
            feed = parse_realtime()
            matches = correlate(feed, trips, route_id=route_id)
            print(f"\nRealtime entiteta: {len(matches)}")
            gps_count = 0
            scheduled_no_gps = 0
            for m in matches[:50]:
                print(f"\nEntity {m.get('entity_id')} trip {m.get('trip_id')}")
                for l in humanize_trip(m.get('trip_update'), stops):
                    print(l)
                for l in humanize_vehicle(m.get('vehicle'), voznipark):
                    print(l)
                if m.get('vehicle') and getattr(m.get('vehicle'), 'position', None):
                    gps_count += 1
                elif m.get('trip_update') is not None:
                    scheduled_no_gps += 1
            print(f"\nGPS opremljena vozila: {gps_count}")
            print(f"Raspoređeni tripovi bez GPS-a: {scheduled_no_gps}")
        except Exception as e:
            print(f"Ne mogu dohvatiti realtime: {e}")

def search_by_vehicle(trips, stops, mode, voznipark):
    vid = safe_input("Unesite garažni broj ili registraciju (npr. 432 ili ZG-8801-GR): ").strip()
    divider(f"Pretraga vozila: {vid}")
    
    found = [v for v in voznipark if (v["garazni"] and str(v["garazni"]) == vid) or 
                                     (v["reg"] and vid.lower() in v["reg"].lower()) or
                                     (v["raw"] and vid.lower() in v["raw"].lower())]
    
    if not found:
        print("Vozilo nije pronađeno u voznipark.txt.")
    else:
        for f in found:
            print(f" Garažni broj: {f.get('garazni') or '-'}")
            print(f" Registracija: {f.get('reg') or '-'}")
            print(f" Model: {f.get('model') or f.get('raw')}")
            print("-"*40)
            
    if mode in ("1","3"):
        try:
            feed = parse_realtime()
            matches = correlate(feed, trips, vehicle_id=vid)
            print(f"Realtime entiteta za vozilo: {len(matches)}")
            for m in matches:
                for l in humanize_trip(m.get('trip_update'), stops):
                    print(l)
                for l in humanize_vehicle(m.get('vehicle'), voznipark):
                    print(l)
        except Exception as e:
            print(f"Ne mogu dohvatiti realtime: {e}")

def search_by_stop_id(trips, stop_times, stops, calendar, calendar_dates, mode, user_dt):
    sid = safe_input("Unesite stop_id (ID stanice iz stops.txt): ").strip()
    divider(f"Stanica {sid}")
    
    stop_name = sid
    if stops is not None and "stop_id" in stops.columns and "stop_name" in stops.columns:
        r = stops[stops["stop_id"] == sid]
        if not r.empty:
            stop_name = r.iloc[0]["stop_name"]
            divider(f"Stanica: {stop_name} ({sid})")

    if mode in ("2","3"):
        if stop_times is None or trips is None or not user_dt:
            print("Nedostaju statički podaci ili datum/vrijeme.")
        else:
            sts = stop_times[stop_times["stop_id"] == sid].copy()
            if sts.empty:
                print("Stanica nije pronađena u stop_times.txt.")
            elif "departure_time" in sts.columns:
                sts["dep_sec"] = sts["departure_time"].apply(lambda t: time_str_to_seconds(t) if isinstance(t, str) else None)
                sec = user_dt.hour*3600 + user_dt.minute*60 + user_dt.second
                merged = sts.merge(trips[["trip_id","route_id","service_id"]] if trips is not None else trips, on="trip_id", how="left")
                active = active_services(calendar, calendar_dates, user_dt.date())
                if "service_id" in merged.columns and active:
                    merged = merged[merged["service_id"].isin(active)]
                merged["delta"] = merged["dep_sec"].apply(lambda x: x - sec if x is not None else 999999)
                upcoming = merged[merged["delta"] >= -300].sort_values("delta").head(50)
                print(f"Statički raspored za {user_dt.strftime('%H:%M')}:")
                for _, r in upcoming.iterrows():
                    dep = int(r["dep_sec"]) if r["dep_sec"] is not None else None
                    hh = f"{dep//3600:02d}:{(dep%3600)//60:02d}" if dep is not None else "-"
                    print(f" route {r.get('route_id')} trip {r.get('trip_id')} dep {hh} delta {int(r['delta'])}s")

    if mode in ("1","3"):
        try:
            feed = parse_realtime()
            matches = correlate(feed, trips, stop_id=sid)
            print(f"\nRealtime entiteta: {len(matches)}")
            for m in matches:
                for l in humanize_trip(m.get('trip_update'), stops):
                    print(l)
                for l in humanize_vehicle(m.get('vehicle'), []): # Vehicle model nije primaran za stop ID
                    print(l)
        except Exception as e:
            print(f"Ne mogu dohvatiti realtime: {e}")

def search_by_stop_name(trips, stop_times, stops, calendar, calendar_dates, mode, user_dt):
    q = safe_input("Unesite dio imena stanice (npr. 'Glavni kolodvor'): ").strip()
    results = find_stops_by_name(stops, q)
    
    if not results:
        print("Nema stanica koje odgovaraju upitu.")
        return
        
    if len(results) == 1:
        chosen = results[0]
    else:
        print("Pronađeno više stanica:")
        for i, r in enumerate(results):
            print(f"[{i}] {r.get('stop_id')} - {r.get('stop_name')}")
        sel = safe_input("Odaberite indeks (enter za 0): ").strip()
        try:
            idx = int(sel) if sel else 0
        except Exception:
            idx = 0
        if idx >= len(results):
            print("Nevažeći indeks.")
            return
        chosen = results[idx]
        
    sid = chosen.get("stop_id")
    divider(f"Stanica: {chosen.get('stop_name')} ({sid})")
    
    # Ponovna upotreba logike za pretragu po stop_id
    if mode in ("2","3"):
        if stop_times is None or trips is None or not user_dt:
            print("Nedostaju statički podaci ili datum/vrijeme.")
        else:
            sts = stop_times[stop_times["stop_id"] == sid].copy()
            if sts.empty:
                print("Stanica nema polazaka u stop_times.txt.")
            elif "departure_time" in sts.columns:
                sts["dep_sec"] = sts["departure_time"].apply(lambda t: time_str_to_seconds(t) if isinstance(t, str) else None)
                sec = user_dt.hour*3600 + user_dt.minute*60 + user_dt.second if user_dt else 0
                merged = sts.merge(trips[["trip_id","route_id","service_id"]] if trips is not None else trips, on="trip_id", how="left")
                active = active_services(calendar, calendar_dates, user_dt.date()) if user_dt else set()
                if "service_id" in merged.columns and active:
                    merged = merged[merged["service_id"].isin(active)]
                merged["delta"] = merged["dep_sec"].apply(lambda x: x - sec if x is not None else 999999)
                upcoming = merged[merged["delta"] >= -300].sort_values("delta").head(50)
                print(f"Statički raspored za {user_dt.strftime('%H:%M') if user_dt else '00:00'}:")
                for _, r in upcoming.iterrows():
                    dep = int(r["dep_sec"]) if r["dep_sec"] is not None else None
                    hh = f"{dep//3600:02d}:{(dep%3600)//60:02d}" if dep is not None else "-"
                    print(f" route {r.get('route_id')} trip {r.get('trip_id')} dep {hh} delta {int(r['delta'])}s")
    
    if mode in ("1","3"):
        try:
            feed = parse_realtime()
            matches = correlate(feed, trips, stop_id=sid)
            print(f"\nRealtime entiteta: {len(matches)}")
            for m in matches:
                for l in humanize_trip(m.get('trip_update'), stops):
                    print(l)
                for l in humanize_vehicle(m.get('vehicle'), []):
                    print(l)
        except Exception as e:
            print(f"Ne mogu dohvatiti realtime: {e}")

def show_voznipark_stats(voznipark):
    divider("Statistika voznog parka")
    if not voznipark:
        print("Datoteka voznipark.txt nije pronađena ili je prazna.")
    else:
        total = len(voznipark)
        print(f"Ukupno zapisa u voznipark.txt: {total}")
        sample = voznipark[:50]
        for s in sample:
            print(f" {s.get('garazni') or '-'} / {s.get('reg') or '-'} / {s.get('model') or s.get('raw')}")

if __name__ == "__main__":
    try:
        main_menu()
    except Exception as e:
        print("Neočekivana pogreška:", e)
        safe_input("Pritisnite Enter za izlaz...")
