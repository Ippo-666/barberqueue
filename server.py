"""
BarberQueue (دورك) - Production Real-time Web Server
===================================================
Hybrid PWA + Real-time Backend Engine for Algerian Barbershops.
Features:
- Real-time Server-Sent Events (SSE) for instant cross-device sync.
- REST API for queue management, dynamic service pricing, and anti-spam strikes.
- B2C Grooming Upsell & B2B Wholesale Supplies Engine.
- Daily financial settlement calculation on salon closing.
- Mobile PWA serving with Manifest & Service Worker.
"""

# Unpack embedded templates and static assets automatically if running on cloud without subfolders
try:
    import assets_bundle
except ImportError:
    pass

from flask import Flask, request, jsonify, render_template, Response, send_from_directory, redirect, make_response
import sqlite3
import json
import os
import sys
import time
import uuid
from datetime import datetime, date, timedelta
from queue import Queue
from typing import Dict, List, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from database import get_db_connection, init_db

app = Flask(
    __name__, 
    root_path=BASE_DIR,
    static_folder=os.path.join(BASE_DIR, "static"), 
    template_folder=os.path.join(BASE_DIR, "templates")
)
app.config['SECRET_KEY'] = 'barber-queue-dz-secret-2026'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Auto-initialize database tables on boot (Gunicorn / Serverless / Local)
try:
    init_db()
    print("[Database] Schema verified & seeded successfully!")
except Exception as e:
    print(f"[Database] Schema init warning: {e}")

# Real-time SSE Broadcaster
class EventBroadcaster:
    def __init__(self):
        self.listeners: List[Queue] = []

    def listen(self) -> Queue:
        q = Queue(maxsize=100)
        self.listeners.append(q)
        return q

    def remove(self, q: Queue):
        if q in self.listeners:
            self.listeners.remove(q)

    def broadcast(self, event_type: str, data: Any):
        payload = json.dumps({"event": event_type, "data": data, "timestamp": time.time()})
        msg = f"data: {payload}\n\n"
        # Push to all active listener queues
        for q in list(self.listeners):
            try:
                q.put_nowait(msg)
            except Exception:
                self.remove(q)

broadcaster = EventBroadcaster()


def get_local_ip() -> str:
    try:
        import socket
        hostname = socket.gethostname()
        ips = socket.gethostbyname_ex(hostname)[2]
        local_ips = [ip for ip in ips if not ip.startswith('127.') and ':' not in ip]
        if local_ips:
            return local_ips[0]
    except Exception:
        pass
    return "127.0.0.1"


# -------------------------------------------------------------
# REST API ENDPOINTS
# -------------------------------------------------------------

@app.route("/api/salon/<salon_id>", methods=["GET"])
def get_salon(salon_id):
    conn = get_db_connection()
    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    if not salon:
        conn.close()
        return jsonify({"error": "Salon not found"}), 404

    services = conn.execute(
        "SELECT * FROM services WHERE salon_id = ? AND is_active = 1", (salon_id,)
    ).fetchall()

    b2c_products = conn.execute(
        "SELECT * FROM products WHERE type = 'b2c_hero'"
    ).fetchall()

    b2b_supplies = conn.execute(
        "SELECT * FROM products WHERE type = 'b2b_supply'"
    ).fetchall()

    conn.close()
    return jsonify({
        "salon": dict(salon),
        "services": [dict(s) for s in services],
        "b2c_products": [dict(p) for p in b2c_products],
        "b2b_supplies": [dict(s) for s in b2b_supplies],
    })


@app.route("/api/queue/<salon_id>", methods=["GET"])
def get_queue(salon_id):
    conn = get_db_connection()
    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    
    # In-chair ticket
    in_chair = None
    if salon and salon["current_in_chair_id"]:
        in_chair_row = conn.execute(
            "SELECT * FROM queue_tickets WHERE id = ?", (salon["current_in_chair_id"],)
        ).fetchone()
        if in_chair_row:
            in_chair = dict(in_chair_row)
            in_chair["services"] = json.loads(in_chair["services_json"])
            in_chair["ordered_products"] = json.loads(in_chair["ordered_products_json"]) if in_chair["ordered_products_json"] else []

    # Waiting tickets ordered by joined_at
    waiting_rows = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? AND status IN ('waiting', 'turn_notified', 'on_the_way', 'traffic_grace', 'arrived')
        ORDER BY joined_at ASC
    """, (salon_id,)).fetchall()

    waiting = []
    accumulated_wait = in_chair["total_duration"] if in_chair else 0
    for row in waiting_rows:
        item = dict(row)
        item["services"] = json.loads(item["services_json"])
        item["ordered_products"] = json.loads(item["ordered_products_json"]) if item["ordered_products_json"] else []
        item["calculated_wait_minutes"] = accumulated_wait
        accumulated_wait += item["total_duration"]

        # Loyalty & style profile lookup
        l_row = conn.execute("SELECT visits_count FROM customer_loyalty WHERE phone = ?", (item["customer_phone"],)).fetchone()
        item["loyalty_visits"] = l_row["visits_count"] if l_row else 1
        h_row = conn.execute("SELECT favorite_cut, notes FROM customer_hair_profiles WHERE phone = ?", (item["customer_phone"],)).fetchone()
        item["saved_cut"] = h_row["favorite_cut"] if h_row else ""
        waiting.append(item)

    if in_chair:
        l_row = conn.execute("SELECT visits_count FROM customer_loyalty WHERE phone = ?", (in_chair["customer_phone"],)).fetchone()
        in_chair["loyalty_visits"] = l_row["visits_count"] if l_row else 1
        h_row = conn.execute("SELECT favorite_cut, notes FROM customer_hair_profiles WHERE phone = ?", (in_chair["customer_phone"],)).fetchone()
        in_chair["saved_cut"] = h_row["favorite_cut"] if h_row else ""

    # Today's completed count
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    completed_count = conn.execute("""
        SELECT COUNT(*) FROM queue_tickets 
        WHERE salon_id = ? AND status = 'completed' AND completed_at >= ?
    """, (salon_id, today_start)).fetchone()[0]

    # Calculate live crowd & rush status (حالة المحل: زحمة بزاف ولا شوية)
    manual_override = "auto"
    if salon and "manual_crowd_override" in salon.keys() and salon["manual_crowd_override"]:
        manual_override = salon["manual_crowd_override"]

    waiting_count = len(waiting)

    # Determine crowd level: Manual Override takes precedence if set, otherwise Auto
    if manual_override and manual_override != "auto":
        effective_level = manual_override
        is_manual = True
    else:
        is_manual = False
        if waiting_count == 0 and not in_chair:
            effective_level = "empty"
        elif waiting_count <= 1:
            effective_level = "low"
        elif waiting_count <= 3:
            effective_level = "medium"
        else:
            effective_level = "high"

    if effective_level in ("empty", "low"):
        crowd_level = effective_level
        crowd_badge = "🟢 فارغ ومتاح فوراً" if (waiting_count == 0 and not in_chair and not is_manual) else "🟢 خفيف (زبائن قلال)"
        crowd_sub = f"{waiting_count} زبائن في الانتظار"
        crowd_title = "حالة المحل: هادئ وخفيف حالياً"
        crowd_desc = "الكرسي متاح أو الانتظار قصير جداً! تفضل بالدخول الآن واحلق بدون تضييع وقت."
        crowd_color = "emerald"
        crowd_meter = 1 if effective_level == "empty" else 2
    elif effective_level == "medium":
        crowd_level = "medium"
        crowd_badge = "🟡 إقبال متوسط (شوية زبائن)"
        crowd_sub = f"{waiting_count} زبائن في الانتظار"
        crowd_title = "حالة المحل: حركة وإقبال متوسط"
        crowd_desc = f"يوجد إقبال معتدل في الصالون (~{accumulated_wait} دقيقة). احجز دورك لتصل مباشرة في موعدك."
        crowd_color = "amber"
        crowd_meter = 3
    else: # high
        crowd_level = "high"
        crowd_badge = "🔴 زحمة بزاف (مكتظ)"
        crowd_sub = f"{waiting_count} زبائن في الطابور"
        crowd_title = "حالة المحل: زحمة واكتظاظ كبير!"
        crowd_desc = f"المحل راه مشجي وفيه زحمة (~{accumulated_wait} دقيقة)! احجز دورك من دارك وتجنب تضييع الوقت بالصالون."
        crowd_color = "rose"
        crowd_meter = 5

    conn.close()
    return jsonify({
        "in_chair": in_chair,
        "waiting": waiting,
        "waiting_count": waiting_count,
        "total_wait_minutes": accumulated_wait,
        "completed_today": completed_count,
        "shop_status": salon["status"] if salon else "open",
        "crowd": {
            "level": crowd_level,
            "badge": crowd_badge,
            "sub": crowd_sub,
            "title": crowd_title,
            "desc": crowd_desc,
            "color": crowd_color,
            "meter": crowd_meter,
            "count": waiting_count,
            "wait_minutes": accumulated_wait,
            "override": manual_override,
            "is_manual": is_manual
        }
    })


@app.route("/api/ticket/<ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    conn = get_db_connection()
    ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (ticket["salon_id"],)).fetchone()

    position = 0
    calculated_wait = 0
    if ticket["status"] in ("waiting", "turn_notified", "on_the_way", "traffic_grace", "arrived"):
        waiting_ahead = conn.execute("""
            SELECT total_duration FROM queue_tickets 
            WHERE salon_id = ? AND status IN ('waiting', 'turn_notified', 'on_the_way', 'traffic_grace', 'arrived')
            AND joined_at < ?
            ORDER BY joined_at ASC
        """, (ticket["salon_id"], ticket["joined_at"])).fetchall()
        position = len(waiting_ahead) + 1
        
        in_chair_dur = 0
        if salon and salon["current_in_chair_id"]:
            ic = conn.execute("SELECT total_duration FROM queue_tickets WHERE id = ?", (salon["current_in_chair_id"],)).fetchone()
            if ic:
                in_chair_dur = ic["total_duration"]

        calculated_wait = in_chair_dur + sum(w["total_duration"] for w in waiting_ahead)

    conn.close()

    res = dict(ticket)
    res["services"] = json.loads(ticket["services_json"]) if ticket["services_json"] else []
    res["ordered_products"] = json.loads(ticket["ordered_products_json"]) if ticket["ordered_products_json"] else []
    res["position"] = position
    res["calculated_wait_minutes"] = calculated_wait
    return jsonify(res)


@app.route("/api/queue/join", methods=["POST"])
def join_queue():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    service_ids = data.get("service_ids", [])
    product_ids = data.get("product_ids", [])

    if not customer_name or not customer_phone or not service_ids:
        return jsonify({"error": "يرجى إدخال الاسم ورقم الهاتف وتحديد خدمة واحدة على الأقل"}), 400

    conn = get_db_connection()

    # 0. Check if salon subscription is suspended
    salon_check = conn.execute("SELECT subscription_status FROM salons WHERE id = ?", (salon_id,)).fetchone()
    if salon_check and salon_check["subscription_status"] == "suspended":
        conn.close()
        return jsonify({"error": "عفواً، الصالون متوقف عن استقبال الحجوزات الإلكترونية مؤقتاً بسبب تعليق الاشتراك"}), 403

    # 1. Anti-spam strike check
    strike_row = conn.execute("SELECT * FROM customer_strikes WHERE phone = ?", (customer_phone,)).fetchone()
    if strike_row and strike_row["is_blocked"]:
        conn.close()
        return jsonify({"error": "تم حظر هذا الرقم مؤقتاً بسبب تكرار الغياب عن المواعيد (3 إنذارات)"}), 403

    # 2. Fetch selected services
    placeholders = ",".join("?" * len(service_ids))
    services_rows = conn.execute(f"SELECT * FROM services WHERE id IN ({placeholders})", service_ids).fetchall()
    
    total_duration = sum(s["duration_minutes"] for s in services_rows)
    total_service_price = sum(s["price"] for s in services_rows)
    services_list = [{"id": s["id"], "name": s["name"], "price": s["price"], "duration": s["duration_minutes"]} for s in services_rows]

    # 3. Fetch selected upsell products
    ordered_products = []
    if product_ids:
        prod_placeholders = ",".join("?" * len(product_ids))
        prod_rows = conn.execute(f"SELECT * FROM products WHERE id IN ({prod_placeholders})", product_ids).fetchall()
        for p in prod_rows:
            ordered_products.append({"id": p["id"], "name": p["name"], "price": p["price"], "icon": p["icon"]})

    ticket_id = f"ticket_{uuid.uuid4().hex[:8]}"

    # Preferred chair & style profile & payment method
    preferred_chair_id = int(data.get("preferred_chair_id") or 0)
    preferred_barber_name = (data.get("preferred_barber_name") or "أي حلاق متاح").strip()
    hair_style_note = (data.get("hair_style_note") or "").strip()
    payment_method = (data.get("payment_method") or "cash").strip()

    # Happy Hour discount check
    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    discount_applied = 0.0
    if salon and salon["happy_hour_active"]:
        discount_applied = float(salon["happy_hour_discount"] or 100.0)
        total_service_price = max(0.0, total_service_price - discount_applied)

    status = "waiting"

    conn.execute("""
    INSERT INTO queue_tickets (
        id, salon_id, customer_name, customer_phone, services_json, 
        total_duration, total_service_price, status, ordered_products_json,
        preferred_chair_id, preferred_barber_name, hair_style_note, payment_method, discount_applied
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticket_id, salon_id, customer_name, customer_phone, 
        json.dumps(services_list, ensure_ascii=False),
        total_duration, total_service_price, status,
        json.dumps(ordered_products, ensure_ascii=False) if ordered_products else None,
        preferred_chair_id, preferred_barber_name, hair_style_note, payment_method, discount_applied
    ))

    # Save hair style preference and register loyalty
    if hair_style_note:
        conn.execute("""
            INSERT INTO customer_hair_profiles (phone, customer_name, favorite_cut, notes, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
                customer_name = excluded.customer_name,
                favorite_cut = excluded.favorite_cut,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
        """, (customer_phone, customer_name, hair_style_note, hair_style_note))

    conn.execute("""
        INSERT INTO customer_loyalty (phone, customer_name, visits_count)
        VALUES (?, ?, 0)
        ON CONFLICT(phone) DO UPDATE SET
            customer_name = excluded.customer_name
    """, (customer_phone, customer_name))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {
        "salon_id": salon_id, 
        "action": "join", 
        "ticket_id": ticket_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "services": services_list,
        "total_service_price": total_service_price,
        "preferred_chair_id": preferred_chair_id,
        "preferred_barber_name": preferred_barber_name,
        "hair_style_note": hair_style_note,
        "discount_applied": discount_applied
    })

    return jsonify({
        "success": True,
        "ticket_id": ticket_id,
        "discount_applied": discount_applied,
        "message": f"مرحباً بك {customer_name}! تم حجز دورك بنجاح." + (f" (تم تطبيق تخفيض الساعات الهادئة: -{discount_applied:.0f} د.ج)" if discount_applied > 0 else "")
    })


@app.route("/api/queue/cancel", methods=["POST"])
def cancel_queue_ticket():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    salon_id = ticket["salon_id"]
    conn.execute("UPDATE queue_tickets SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP WHERE id = ?", (ticket_id,))
    
    # If it was in chair, clear chair
    conn.execute("UPDATE salons SET current_in_chair_id = NULL WHERE id = ? AND current_in_chair_id = ?", (salon_id, ticket_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {
        "salon_id": salon_id,
        "action": "cancel",
        "ticket_id": ticket_id
    })
    return jsonify({"success": True, "message": "تم إلغاء التذكرة بنجاح"})


@app.route("/api/queue/next", methods=["POST"])
def call_next():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")

    conn = get_db_connection()
    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()

    # 1. Complete any previous in-chair tickets
    conn.execute("""
        UPDATE queue_tickets 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
        WHERE salon_id = ? AND status = 'in_chair'
    """, (salon_id,))

    # 2. Pick next waiting client
    next_client = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? AND status IN ('waiting', 'turn_notified', 'on_the_way', 'traffic_grace', 'arrived')
        ORDER BY joined_at ASC LIMIT 1
    """, (salon_id,)).fetchone()

    next_id = None
    if next_client:
        next_id = next_client["id"]
        conn.execute("UPDATE queue_tickets SET status = 'in_chair' WHERE id = ?", (next_id,))
        conn.execute("UPDATE salons SET current_in_chair_id = ? WHERE id = ?", (next_id, salon_id))
    else:
        conn.execute("UPDATE salons SET current_in_chair_id = NULL WHERE id = ?", (salon_id,))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "next", "in_chair_id": next_id})
    return jsonify({"success": True, "in_chair_id": next_id})


# -------------------------------------------------------------
# MULTI-CHAIR & PERFORMANCE ANALYTICS ENDPOINTS
# -------------------------------------------------------------

@app.route("/api/barber/chairs/<salon_id>", methods=["GET"])
def get_salon_chairs(salon_id):
    conn = get_db_connection()
    chairs_rows = conn.execute("""
        SELECT * FROM salon_chairs 
        WHERE salon_id = ? AND is_active = 1 
        ORDER BY chair_number ASC
    """, (salon_id,)).fetchall()

    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    chairs_data = []

    for c in chairs_rows:
        chair = dict(c)
        chair_num = chair["chair_number"]
        
        # Load current active client in this chair if any
        current_client = None
        if chair["current_ticket_id"]:
            t_row = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (chair["current_ticket_id"],)).fetchone()
            if t_row and t_row["status"] == "in_chair":
                current_client = dict(t_row)
                current_client["services"] = json.loads(current_client["services_json"]) if current_client["services_json"] else []
                current_client["ordered_products"] = json.loads(current_client["ordered_products_json"]) if current_client["ordered_products_json"] else []
                l_row = conn.execute("SELECT visits_count FROM customer_loyalty WHERE phone = ?", (current_client["customer_phone"],)).fetchone()
                current_client["loyalty_visits"] = l_row["visits_count"] if l_row else 1
            else:
                conn.execute("UPDATE salon_chairs SET current_ticket_id = NULL WHERE id = ?", (chair["id"],))
                conn.commit()

        chair["current_client"] = current_client

        # Today's completed count for this chair
        completed_today = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_service_price), 0) 
            FROM queue_tickets 
            WHERE salon_id = ? AND chair_id = ? AND status = 'completed' AND completed_at >= ?
        """, (salon_id, chair_num, today_start)).fetchone()

        haircuts_count = completed_today[0]
        revenue = float(completed_today[1])
        commission_rate = chair.get("commission_rate") or 50.0
        barber_share = revenue * (commission_rate / 100.0)
        salon_share = revenue - barber_share

        chair["today_haircuts"] = haircuts_count
        chair["today_revenue"] = revenue
        chair["barber_share"] = barber_share
        chair["salon_share"] = salon_share
        chairs_data.append(chair)

    # Compute rankings
    sorted_by_haircuts = sorted(chairs_data, key=lambda x: x["today_haircuts"], reverse=True)
    for rank, ch in enumerate(sorted_by_haircuts, 1):
        for orig in chairs_data:
            if orig["id"] == ch["id"]:
                orig["rank_haircuts"] = rank

    sorted_by_revenue = sorted(chairs_data, key=lambda x: x["today_revenue"], reverse=True)
    for rank, ch in enumerate(sorted_by_revenue, 1):
        for orig in chairs_data:
            if orig["id"] == ch["id"]:
                orig["rank_revenue"] = rank

    total_revenue = sum(c["today_revenue"] for c in chairs_data)
    total_haircuts = sum(c["today_haircuts"] for c in chairs_data)

    for ch in chairs_data:
        ch["revenue_percentage"] = round((ch["today_revenue"] / total_revenue * 100), 1) if total_revenue > 0 else 0

    conn.close()
    return jsonify({
        "chairs": chairs_data,
        "total_revenue": total_revenue,
        "total_haircuts": total_haircuts,
        "chairs_count": len(chairs_data)
    })


@app.route("/api/barber/chairs/add", methods=["POST"])
def add_salon_chair():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    barber_name = (data.get("barber_name") or "").strip()
    commission_rate = float(data.get("commission_rate") or 50.0)

    if not barber_name:
        return jsonify({"error": "يرجى إدخال اسم الحلاق المسؤول عن الكرسي"}), 400

    conn = get_db_connection()
    max_num = conn.execute("SELECT COALESCE(MAX(chair_number), 0) FROM salon_chairs WHERE salon_id = ?", (salon_id,)).fetchone()[0]
    next_num = max_num + 1
    chair_id = f"chair_{salon_id}_{next_num}"

    conn.execute("""
        INSERT INTO salon_chairs (id, salon_id, chair_number, barber_name, commission_rate)
        VALUES (?, ?, ?, ?, ?)
    """, (chair_id, salon_id, next_num, barber_name, commission_rate))
    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "add", "chair_id": chair_id})
    return jsonify({"success": True, "chair_id": chair_id, "chair_number": next_num, "message": f"تمت إضافة كرسي {next_num} ({barber_name}) بنجاح!"})


@app.route("/api/barber/chairs/call-next", methods=["POST"])
def chair_call_next():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    chair_id = data.get("chair_id")

    conn = get_db_connection()
    chair = conn.execute("SELECT * FROM salon_chairs WHERE id = ?", (chair_id,)).fetchone()
    if not chair:
        conn.close()
        return jsonify({"error": "الكرسي غير موجود"}), 404

    chair_num = chair["chair_number"]

    # 1. Complete previous client in this chair if any
    if chair["current_ticket_id"]:
        conn.execute("""
            UPDATE queue_tickets 
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'in_chair'
        """, (chair["current_ticket_id"],))
        prev_t = conn.execute("SELECT customer_phone, customer_name FROM queue_tickets WHERE id = ?", (chair["current_ticket_id"],)).fetchone()
        if prev_t and prev_t["customer_phone"]:
            conn.execute("""
                INSERT INTO customer_loyalty (phone, customer_name, visits_count, last_visit_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(phone) DO UPDATE SET
                    visits_count = visits_count + 1,
                    last_visit_at = CURRENT_TIMESTAMP
            """, (prev_t["customer_phone"], prev_t["customer_name"]))

    # 2. Pick next waiting client (respecting preferred barber/chair if chosen)
    next_client = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? 
          AND status IN ('waiting', 'turn_notified', 'on_the_way', 'traffic_grace', 'arrived')
          AND (preferred_chair_id = 0 OR preferred_chair_id = ? OR preferred_chair_id IS NULL)
        ORDER BY joined_at ASC LIMIT 1
    """, (salon_id, chair_num)).fetchone()

    next_id = None
    if next_client:
        next_id = next_client["id"]
        conn.execute("UPDATE queue_tickets SET status = 'in_chair', chair_id = ? WHERE id = ?", (chair_num, next_id))
        conn.execute("UPDATE salon_chairs SET current_ticket_id = ? WHERE id = ?", (next_id, chair_id))
        if chair_num == 1:
            conn.execute("UPDATE salons SET current_in_chair_id = ? WHERE id = ?", (next_id, salon_id))
    else:
        conn.execute("UPDATE salon_chairs SET current_ticket_id = NULL WHERE id = ?", (chair_id,))
        if chair_num == 1:
            conn.execute("UPDATE salons SET current_in_chair_id = NULL WHERE id = ?", (salon_id,))

    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "call_next", "chair_id": chair_id, "ticket_id": next_id})
    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "call_next", "chair_id": chair_id, "ticket_id": next_id})
    if next_id:
        broadcaster.broadcast("turn_alert", {"salon_id": salon_id, "ticket_id": next_id, "chair_number": chair_num, "barber_name": chair["barber_name"]})

    return jsonify({"success": True, "in_chair_id": next_id, "chair_number": chair_num})


@app.route("/api/barber/chairs/complete", methods=["POST"])
def chair_complete_service():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    chair_id = data.get("chair_id")

    conn = get_db_connection()
    chair = conn.execute("SELECT * FROM salon_chairs WHERE id = ?", (chair_id,)).fetchone()
    if not chair:
        conn.close()
        return jsonify({"error": "الكرسي غير موجود"}), 404

    ticket_id = chair["current_ticket_id"]
    if ticket_id:
        conn.execute("""
            UPDATE queue_tickets 
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (ticket_id,))
        prev_t = conn.execute("SELECT customer_phone, customer_name FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if prev_t and prev_t["customer_phone"]:
            conn.execute("""
                INSERT INTO customer_loyalty (phone, customer_name, visits_count, last_visit_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(phone) DO UPDATE SET
                    visits_count = visits_count + 1,
                    last_visit_at = CURRENT_TIMESTAMP
            """, (prev_t["customer_phone"], prev_t["customer_name"]))

    conn.execute("UPDATE salon_chairs SET current_ticket_id = NULL WHERE id = ?", (chair_id,))
    if chair["chair_number"] == 1:
        conn.execute("UPDATE salons SET current_in_chair_id = NULL WHERE id = ?", (salon_id,))

    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "complete", "chair_id": chair_id})
    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "complete", "chair_id": chair_id})
    return jsonify({"success": True, "message": "تم إنهاء الحلاقة بنجاح!"})


@app.route("/api/barber/chairs/addon-product", methods=["POST"])
def chair_addon_product():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    chair_id = data.get("chair_id")
    product_id = data.get("product_id")

    if not chair_id or not product_id:
        return jsonify({"error": "chair_id and product_id are required"}), 400

    conn = get_db_connection()
    chair = conn.execute("SELECT * FROM salon_chairs WHERE id = ? AND salon_id = ?", (chair_id, salon_id)).fetchone()
    if not chair or not chair["current_ticket_id"]:
        conn.close()
        return jsonify({"error": "لا يوجد زبون نشط على هذا الكرسي حالياً"}), 400

    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "المنتج غير موجود"}), 404

    ticket_id = chair["current_ticket_id"]
    ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket or ticket["status"] != "in_chair":
        conn.close()
        return jsonify({"error": "التذكرة غير نشطة على الكرسي"}), 400

    ordered_products = []
    if ticket["ordered_products_json"]:
        try:
            ordered_products = json.loads(ticket["ordered_products_json"])
        except Exception:
            ordered_products = []

    ordered_products.append({
        "id": product["id"],
        "name": product["name"],
        "price": float(product["price"]),
        "icon": product["icon"] or "✨"
    })

    new_total_price = float(ticket["total_service_price"] or 0) + float(product["price"])

    conn.execute("""
        UPDATE queue_tickets 
        SET ordered_products_json = ?, total_service_price = ?
        WHERE id = ?
    """, (json.dumps(ordered_products, ensure_ascii=False), new_total_price, ticket_id))

    conn.execute("UPDATE products SET stock_count = MAX(0, stock_count - 1) WHERE id = ?", (product["id"],))

    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "product_added", "chair_id": chair_id})
    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "product_added", "ticket_id": ticket_id})

    return jsonify({
        "success": True,
        "message": f"تمت إضافة ({product['name']}) بنجاح! السعر الإجمالي: {new_total_price:,.0f} د.ج",
        "new_total_price": new_total_price,
        "product_name": product["name"]
    })


@app.route("/api/barber/chairs/delete", methods=["POST"])
def delete_salon_chair():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    chair_id = data.get("chair_id")

    conn = get_db_connection()
    chair = conn.execute("SELECT * FROM salon_chairs WHERE id = ?", (chair_id,)).fetchone()
    if not chair:
        conn.close()
        return jsonify({"error": "الكرسي غير موجود"}), 404

    if chair["chair_number"] == 1:
        conn.close()
        return jsonify({"error": "لا يمكن حذف الكرسي الرئيسي (كرسي 1)"}), 400

    conn.execute("DELETE FROM salon_chairs WHERE id = ?", (chair_id,))
    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "delete", "chair_id": chair_id})
    return jsonify({"success": True, "message": "تم حذف الكرسي بنجاح"})


@app.route("/api/queue/walkin", methods=["POST"])
@app.route("/api/barber/walkin", methods=["POST"])
def add_walkin():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    service_name = data.get("service_name", "حلاقة شعر كلاسيكية")
    duration = int(data.get("duration", 20))
    price = float(data.get("price", 500.0))
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "0000000000").strip()
    status = data.get("status", "waiting")

    conn = get_db_connection()
    ticket_id = f"walkin_{uuid.uuid4().hex[:6]}"
    if not customer_name:
        customer_name = f"زبون حضوري (مقعد {ticket_id[-3:]})"

    services_list = [{"id": "srv_walkin", "name": service_name, "price": price, "duration": duration}]

    if status == "completed":
        conn.execute("""
        INSERT INTO queue_tickets (
            id, salon_id, customer_name, customer_phone, services_json, 
            total_duration, total_service_price, status, is_walkin, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 1, CURRENT_TIMESTAMP)
        """, (
            ticket_id, salon_id, customer_name, customer_phone,
            json.dumps(services_list, ensure_ascii=False),
            duration, price
        ))
    else:
        conn.execute("""
        INSERT INTO queue_tickets (
            id, salon_id, customer_name, customer_phone, services_json, 
            total_duration, total_service_price, status, is_walkin
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', 1)
        """, (
            ticket_id, salon_id, customer_name, customer_phone,
            json.dumps(services_list, ensure_ascii=False),
            duration, price
        ))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "walkin", "duration": duration, "ticket_id": ticket_id})
    return jsonify({
        "success": True, 
        "ticket_id": ticket_id,
        "customer_name": customer_name,
        "message": f"تم تسجيل الزبون الحضوري ({duration} دقيقة) بنجاح"
    })


@app.route("/api/queue/grace-period", methods=["POST"])
def request_grace_period():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    minutes = int(data.get("minutes", 10))

    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    expires_at = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        UPDATE queue_tickets 
        SET status = 'traffic_grace', traffic_grace_minutes = ?, grace_expires_at = ?
        WHERE id = ?
    """, (minutes, expires_at, ticket_id))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"action": "grace_period", "ticket_id": ticket_id, "minutes": minutes})
    return jsonify({"success": True, "message": f"تم تفعيل مهلة الزحمة ({minutes} دقائق) وتنبيه الحلاق"})


@app.route("/api/queue/on-the-way", methods=["POST"])
def confirm_on_the_way():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    arrival_minutes = int(data.get("arrival_minutes", 5))

    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    conn.execute("""
        UPDATE queue_tickets 
        SET status = 'on_the_way', estimated_arrival_minutes = ?, traffic_grace_minutes = 0, grace_expires_at = NULL
        WHERE id = ?
    """, (arrival_minutes, ticket_id))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"action": "on_the_way", "ticket_id": ticket_id, "minutes": arrival_minutes})
    return jsonify({"success": True, "message": "تم إشعار الحلاق بأنك في الطريق للصالون"})


@app.route("/api/queue/notify-turn", methods=["POST"])
def notify_turn():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    salon_id = data.get("salon_id", "salon_dz_1")
    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE queue_tickets 
        SET status = 'turn_notified', alert_sent_at = ? 
        WHERE id = ?
    """, (now_str, ticket_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("turn_alert", {
        "salon_id": salon_id,
        "ticket_id": ticket_id,
        "customer_name": ticket["customer_name"],
        "message": f"🔔 تنبيه من الحلاق: دورك اقترب جداً يا {ticket['customer_name']}! يرجى الانطلاق للصالون الآن."
    })
    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "notify_turn", "ticket_id": ticket_id})

    return jsonify({"success": True, "message": f"تم إرسال تنبيه اقتراب الدور لهاتف الزبون ({ticket['customer_name']}) بنجاح!"})


@app.route("/api/queue/arrived", methods=["POST"])
def confirm_arrived():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    conn.execute("UPDATE queue_tickets SET status = 'arrived' WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"action": "arrived", "ticket_id": ticket_id})
    return jsonify({"success": True, "message": "تم إشعار الحلاق بأنك متواجد حالياً في قاعة الانتظار!"})


@app.route("/api/queue/filler", methods=["POST"])
def insert_filler_service():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    service_name = data.get("service_name", "خدمة سريعة ريثما يصل الزبون (تحديد/ماسك)")
    duration = int(data.get("duration", 10))
    price = float(data.get("price", 300.0))

    conn = get_db_connection()
    filler_id = f"filler_{uuid.uuid4().hex[:6]}"
    filler_services = json.dumps([{"id": "srv_filler", "name": service_name, "price": price, "duration": duration}], ensure_ascii=False)

    conn.execute("""
    INSERT INTO queue_tickets (
        id, salon_id, customer_name, customer_phone, services_json, 
        total_duration, total_service_price, status, is_walkin, completed_at
    ) VALUES (?, ?, 'خدمة سريعة (ريثما يصل الزبون)', '0000000000', ?, ?, ?, 'completed', 1, CURRENT_TIMESTAMP)
    """, (filler_id, salon_id, filler_services, duration, price))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {
        "salon_id": salon_id,
        "action": "filler",
        "notice": f"ℹ️ ملاحظة للزبائن: الحلاق يقوم بخدمة سريعة ({duration} دقائق) ريثما يصل الزبون التالي."
    })
    return jsonify({"success": True, "message": f"تم تسجيل الخدمة السريعة ({duration} دقيقة) بنجاح دون إهدار الوقت!"})


@app.route("/api/queue/companion", methods=["POST"])
def add_companion():
    data = request.json or {}
    parent_ticket_id = data.get("parent_ticket_id")
    companion_name = data.get("companion_name", "مرافق (ابن / صديق)")
    service_id = data.get("service_id")
    salon_id = data.get("salon_id", "salon_dz_1")

    conn = get_db_connection()
    parent = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (parent_ticket_id,)).fetchone()
    if not parent:
        conn.close()
        return jsonify({"error": "تذكرة الدور الأصلية غير موجودة"}), 400

    target_salon_id = parent["salon_id"] if parent else salon_id
    srv = None
    if service_id:
        srv = conn.execute("SELECT * FROM services WHERE id = ? AND salon_id = ?", (service_id, target_salon_id)).fetchone()
    if not srv:
        # Fallback to first available service of this salon
        srv = conn.execute("SELECT * FROM services WHERE salon_id = ? ORDER BY id ASC LIMIT 1", (target_salon_id,)).fetchone()
    if not srv:
        srv = {"id": "srv_haircut", "name": "قص شعر كلاسيكي", "price": 400.0, "duration_minutes": 20}
    else:
        srv = dict(srv)

    cid = f"comp_{uuid.uuid4().hex[:6]}"
    dur = int(srv.get("duration_minutes") or srv.get("duration") or 20)
    prc = float(srv.get("price") or 400.0)
    srv_json = json.dumps([{"id": srv.get("id", "srv_haircut"), "name": srv.get("name", "قص شعر"), "price": prc, "duration": dur}], ensure_ascii=False)

    conn.execute("""
    INSERT INTO queue_tickets (
        id, salon_id, customer_name, customer_phone, services_json,
        total_duration, total_service_price, status, has_companion
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', 1)
    """, (cid, target_salon_id, f"{companion_name} (مع {parent['customer_name']})", parent["customer_phone"], srv_json, dur, prc))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {"salon_id": target_salon_id, "action": "companion"})
    return jsonify({"success": True, "companion_id": cid, "message": f"تم حجز دور للمرافق ({companion_name}) معك بنجاح!"})


@app.route("/api/queue/strike", methods=["POST"])
def apply_strike():
    data = request.json or {}
    ticket_id = data.get("ticket_id")
    block_now = data.get("block_now", True)

    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    conn = get_db_connection()
    ticket = conn.execute("SELECT * FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    phone = ticket["customer_phone"]
    name = ticket["customer_name"]
    salon_id = ticket["salon_id"]

    # Mark ticket as no_show
    conn.execute("UPDATE queue_tickets SET status = 'no_show' WHERE id = ?", (ticket_id,))

    # Update strikes and BLOCK the phone number
    existing_strike = conn.execute("SELECT * FROM customer_strikes WHERE phone = ?", (phone,)).fetchone()
    strike_count = 1
    if existing_strike:
        strike_count = existing_strike["strike_count"] + 1
        is_blocked = 1 if block_now else (1 if strike_count >= 2 else 0)
        conn.execute("""
            UPDATE customer_strikes 
            SET strike_count = ?, is_blocked = ?, last_strike_at = CURRENT_TIMESTAMP 
            WHERE phone = ?
        """, (strike_count, is_blocked, phone))
    else:
        is_blocked = 1 if block_now else 0
        conn.execute("""
            INSERT INTO customer_strikes (phone, customer_name, strike_count, is_blocked, last_strike_at)
            VALUES (?, ?, 1, ?, CURRENT_TIMESTAMP)
        """, (phone, name, is_blocked))

    conn.commit()
    conn.close()

    broadcaster.broadcast("queue_updated", {
        "action": "strike_and_block",
        "ticket_id": ticket_id,
        "phone": phone,
        "customer_name": name,
        "is_blocked": is_blocked,
        "strike_count": strike_count,
        "salon_id": salon_id
    })
    return jsonify({
        "success": True, 
        "strike_count": strike_count, 
        "is_blocked": bool(is_blocked), 
        "customer_name": name,
        "message": f"تم تسجيل إنذار وحظر الزبون {name} ({phone}) بنجاح!" if is_blocked else f"تم إنذار الزبون {name}"
    })


@app.route("/api/customer/banned", methods=["GET"])
def get_banned_customers():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM customer_strikes WHERE is_blocked = 1 ORDER BY last_strike_at DESC").fetchall()
    conn.close()
    return jsonify({"banned": [dict(r) for r in rows]})


@app.route("/api/customer/unblock", methods=["POST"])
def unblock_customer():
    data = request.json or {}
    phone = data.get("phone")
    if not phone:
        return jsonify({"error": "Missing phone"}), 400
    conn = get_db_connection()
    conn.execute("UPDATE customer_strikes SET is_blocked = 0, strike_count = 0 WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()
    broadcaster.broadcast("queue_updated", {"action": "unblocked", "phone": phone})
    return jsonify({"success": True, "message": f"تم فك الحظر عن الرقم {phone} بنجاح!"})


@app.route("/api/barber/toggle-status", methods=["POST"])
def toggle_salon_status():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    conn = get_db_connection()
    salon = conn.execute("SELECT status FROM salons WHERE id = ?", (salon_id,)).fetchone()
    current_status = salon["status"] if salon else "open"
    new_status = "closed" if current_status == "open" else "open"
    conn.execute("UPDATE salons SET status = ? WHERE id = ?", (new_status, salon_id))
    conn.commit()
    conn.close()
    broadcaster.broadcast("salon_status_changed", {"salon_id": salon_id, "status": new_status})
    return jsonify({"success": True, "status": new_status, "message": f"حالة الصالون الآن: {new_status}"})


@app.route("/api/barber/crowd-override", methods=["POST"])
def set_crowd_override():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    override = data.get("override", "auto")
    if override not in ("auto", "low", "medium", "high"):
        return jsonify({"error": "قيمة غير صالحة"}), 400

    conn = get_db_connection()
    conn.execute("UPDATE salons SET manual_crowd_override = ? WHERE id = ?", (override, salon_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("crowd_updated", {
        "salon_id": salon_id,
        "override": override
    })
    broadcaster.broadcast("queue_updated", {
        "salon_id": salon_id,
        "action": "crowd_override",
        "override": override
    })
    return jsonify({"success": True, "override": override, "message": f"تم ضبط حالة ضغط المحل إلى: {override}"})


@app.route("/api/barber/eid-mode", methods=["POST"])
def toggle_eid_mode():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")

    conn = get_db_connection()
    current = conn.execute("SELECT is_eid_mode FROM salons WHERE id = ?", (salon_id,)).fetchone()
    cur_val = current["is_eid_mode"] if current and current["is_eid_mode"] is not None else 0
    new_val = 1 if cur_val == 0 else 0

    conn.execute("UPDATE salons SET is_eid_mode = ? WHERE id = ?", (new_val, salon_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("eid_mode_updated", {"salon_id": salon_id, "is_eid_mode": bool(new_val)})
    return jsonify({
        "success": True, 
        "is_eid_mode": bool(new_val),
        "message": f"تم {'تفعيل 🚀' if new_val else 'إلغاء ⏸️'} وضع ضغط الأعياد بنجاح!"
    })


@app.route("/api/salon/services", methods=["POST"])
def update_services():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    services = data.get("services", [])

    conn = get_db_connection()
    for srv in services:
        name = str(srv.get("name") or "خدمة حلاقة").strip()
        price = float(srv.get("price") or 400.0)
        dur = int(srv.get("duration_minutes") or srv.get("duration") or 20)
        icon = str(srv.get("icon") or "✂️")

        if srv.get("is_new"):
            srv_id = f"srv_{uuid.uuid4().hex[:6]}"
            conn.execute("""
                INSERT INTO services (id, salon_id, name, duration_minutes, price, icon)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (srv_id, salon_id, name, dur, price, icon))
        else:
            srv_id = srv.get("id")
            if srv_id:
                conn.execute("""
                    UPDATE services 
                    SET price = ?, duration_minutes = ? 
                    WHERE id = ? AND salon_id = ?
                """, (price, dur, srv_id, salon_id))

    conn.commit()
    conn.close()

    broadcaster.broadcast("services_updated", {"salon_id": salon_id})
    return jsonify({"success": True, "message": "تم حفظ وتحديث قائمة أسعار ومدد الخدمات بنجاح"})


@app.route("/api/wholesale/order", methods=["POST"])
def create_wholesale_order():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    product_id = data.get("product_id")

    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Product not found"}), 404

    order_id = f"ord_{uuid.uuid4().hex[:6]}"
    conn.execute("""
    INSERT INTO wholesale_orders (
        id, salon_id, product_id, item_name, unit, wholesale_price, platform_profit, status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
    """, (
        order_id, salon_id, product["id"], product["name"],
        product["unit_spec"] or "وحدة", product["price"], product["commission_or_profit"]
    ))

    conn.commit()
    conn.close()

    broadcaster.broadcast("wholesale_order_created", {"salon_id": salon_id, "order_id": order_id, "item": product["name"]})
    return jsonify({
        "success": True, 
        "order_id": order_id,
        "message": f"تم تأكيد طلب تموين: {product['name']} ({product['price']:,.0f} د.ج) - التسليم مع جولة التوزيع الأسبوعية!"
    })


@app.route("/api/barber/settlement/<salon_id>", methods=["GET"])
def get_live_settlement(salon_id):
    today_str = date.today().strftime("%Y-%m-%d")
    conn = get_db_connection()
    today_completed = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? AND status = 'completed' AND DATE(completed_at) = ?
    """, (salon_id, today_str)).fetchall()

    total_clients = len(today_completed)
    walkin_clients = sum(1 for t in today_completed if t["is_walkin"] == 1)
    app_clients = total_clients - walkin_clients
    gross_services_revenue = sum(t["total_service_price"] for t in today_completed)

    products_sold_count = 0
    products_sold_revenue = 0.0
    barber_commission_earned = 0.0

    for t in today_completed:
        if t["ordered_products_json"]:
            try:
                prods = json.loads(t["ordered_products_json"])
                for p in prods:
                    products_sold_count += 1
                    products_sold_revenue += p.get("price", 0.0)
                    barber_commission_earned += 200.0
            except Exception:
                pass

    total_barber_take_home = gross_services_revenue + barber_commission_earned

    # 4. Expenses today (Sorties de Caisse)
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    total_expenses_today = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM salon_expenses 
        WHERE salon_id = ? AND created_at >= ?
    """, (salon_id, today_start)).fetchone()[0]
    total_expenses = float(total_expenses_today)
    net_after_expenses = max(0.0, total_barber_take_home - total_expenses)

    no_shows = conn.execute("""
        SELECT COUNT(*) FROM queue_tickets 
        WHERE salon_id = ? AND status = 'no_show' AND DATE(joined_at) = ?
    """, (salon_id, today_str)).fetchone()[0]

    conn.close()

    return jsonify({
        "date": today_str,
        "total_clients": total_clients,
        "app_clients": app_clients,
        "walkin_clients": walkin_clients,
        "gross_services_revenue": gross_services_revenue,
        "products_sold_count": products_sold_count,
        "products_sold_revenue": products_sold_revenue,
        "barber_product_commission": barber_commission_earned,
        "total_barber_take_home": total_barber_take_home,
        "total_expenses_today": total_expenses,
        "net_after_expenses": net_after_expenses,
        "no_show_strikes": no_shows,
    })


# -------------------------------------------------------------
# 1. SMART HAPPY HOURS ENDPOINT
# -------------------------------------------------------------
@app.route("/api/barber/happy-hour", methods=["POST"])
def toggle_happy_hour():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    active = 1 if (data.get("active") or data.get("is_active")) else 0
    discount = float(data.get("discount") or 100.0)
    note = (data.get("note") or "تخفيض الساعات الهادئة (100 د.ج)").strip()

    conn = get_db_connection()
    conn.execute("""
        UPDATE salons 
        SET happy_hour_active = ?, happy_hour_discount = ?, happy_hour_note = ?
        WHERE id = ?
    """, (active, discount, note, salon_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("happy_hour_updated", {
        "salon_id": salon_id,
        "active": bool(active),
        "discount": discount,
        "note": note
    })
    return jsonify({"success": True, "active": bool(active), "discount": discount, "note": note})


# -------------------------------------------------------------
# 2. BARIDIMOB RIP CONFIGURATION
# -------------------------------------------------------------
@app.route("/api/salon/baridimob", methods=["POST"])
def update_baridimob_rip():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    rip = (data.get("rip") or "").strip()

    conn = get_db_connection()
    conn.execute("UPDATE salons SET baridimob_rip = ? WHERE id = ?", (rip, salon_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("salon_updated", {"salon_id": salon_id, "baridimob_rip": rip})
    return jsonify({"success": True, "baridimob_rip": rip, "message": "تم تحديث رقم بريدي موب بنجاح!"})


# -------------------------------------------------------------
# 3. CUSTOMER LOYALTY & HAIR STYLE PROFILE LOOKUP
# -------------------------------------------------------------
@app.route("/api/customer/profile/<phone>", methods=["GET"])
def get_customer_profile(phone):
    phone_clean = phone.strip()
    conn = get_db_connection()
    loyalty = conn.execute("SELECT * FROM customer_loyalty WHERE phone = ?", (phone_clean,)).fetchone()
    hair = conn.execute("SELECT * FROM customer_hair_profiles WHERE phone = ?", (phone_clean,)).fetchone()
    strike = conn.execute("SELECT * FROM customer_strikes WHERE phone = ?", (phone_clean,)).fetchone()
    conn.close()

    visits = loyalty["visits_count"] if loyalty else 0
    # Loyalty rule: 5 visits = free gift (masque / coupure)
    stamps = visits % 5
    is_reward_ready = (visits > 0 and visits % 5 == 0)

    return jsonify({
        "phone": phone_clean,
        "customer_name": (loyalty["customer_name"] if loyalty else (hair["customer_name"] if hair else "")),
        "visits_count": visits,
        "stamps": stamps,
        "stamps_count": stamps,
        "reward_ready": is_reward_ready,
        "favorite_cut": hair["favorite_cut"] if hair else "",
        "notes": hair["notes"] if hair else "",
        "is_blocked": bool(strike["is_blocked"]) if strike else False
    })


# -------------------------------------------------------------
# 4. SALON EXPENSES (SORTIES DE CAISSE)
# -------------------------------------------------------------
@app.route("/api/barber/expenses/<salon_id>", methods=["GET"])
def get_salon_expenses(salon_id):
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT * FROM salon_expenses 
        WHERE salon_id = ? AND created_at >= ?
        ORDER BY created_at DESC
    """, (salon_id, today_start)).fetchall()
    
    total = sum(float(r["amount"]) for r in rows)
    conn.close()
    return jsonify({
        "expenses": [dict(r) for r in rows],
        "total_today": total,
        "total_expenses": total
    })


@app.route("/api/barber/expenses/add", methods=["POST"])
@app.route("/api/barber/expense", methods=["POST"])
def add_salon_expense():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    amount = float(data.get("amount") or 0.0)
    category = (data.get("category") or "مصروف عام").strip()
    description = (data.get("description") or "").strip()
    recorded_by = (data.get("recorded_by") or "المعلم").strip()

    if amount <= 0:
        return jsonify({"error": "يرجى إدخال مبلغ صحيح للمصروف"}), 400

    exp_id = f"exp_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO salon_expenses (id, salon_id, amount, category, description, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (exp_id, salon_id, amount, category, description, recorded_by))
    conn.commit()
    conn.close()

    broadcaster.broadcast("expense_updated", {"salon_id": salon_id, "amount": amount})
    return jsonify({"success": True, "expense_id": exp_id, "message": "تم تسجيل المصروف بنجاح!"})


@app.route("/api/barber/expenses/delete", methods=["POST"])
def delete_salon_expense():
    data = request.json or {}
    exp_id = data.get("expense_id")
    conn = get_db_connection()
    conn.execute("DELETE FROM salon_expenses WHERE id = ?", (exp_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم حذف المصروف بنجاح"})


@app.route("/api/barber/close-day", methods=["POST"])
def close_day_settlement():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    today_str = date.today().strftime("%Y-%m-%d")

    conn = get_db_connection()

    # 1. Calculate today's completed tickets
    today_completed = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? AND status = 'completed' AND DATE(completed_at) = ?
    """, (salon_id, today_str)).fetchall()

    total_clients = len(today_completed)
    walkin_clients = sum(1 for t in today_completed if t["is_walkin"] == 1)
    app_clients = total_clients - walkin_clients
    gross_services_revenue = sum(t["total_service_price"] for t in today_completed)

    # 2. Calculate products sold
    products_sold_count = 0
    products_sold_revenue = 0.0
    barber_commission_earned = 0.0

    for t in today_completed:
        if t["ordered_products_json"]:
            try:
                prods = json.loads(t["ordered_products_json"])
                for p in prods:
                    products_sold_count += 1
                    products_sold_revenue += p.get("price", 0.0)
                    barber_commission_earned += 200.0  # avg 200 DZD per item
            except Exception:
                pass

    total_barber_take_home = gross_services_revenue + barber_commission_earned

    # 3. No-show strikes today
    no_shows = conn.execute("""
        SELECT COUNT(*) FROM queue_tickets 
        WHERE salon_id = ? AND status = 'no_show' AND DATE(joined_at) = ?
    """, (salon_id, today_str)).fetchone()[0]

    settlement_id = f"stl_{today_str.replace('-', '')}_{salon_id}"

    # Insert or replace settlement
    conn.execute("""
    INSERT OR REPLACE INTO daily_settlements (
        id, salon_id, settlement_date, total_clients, app_clients, walkin_clients,
        gross_services_revenue, products_sold_count, products_sold_revenue,
        barber_product_commission, total_barber_take_home, no_show_strikes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        settlement_id, salon_id, today_str, total_clients, app_clients, walkin_clients,
        gross_services_revenue, products_sold_count, products_sold_revenue,
        barber_commission_earned, total_barber_take_home, no_shows
    ))

    # Toggle shop status to closed
    conn.execute("UPDATE salons SET status = 'closed', current_in_chair_id = NULL WHERE id = ?", (salon_id,))
    conn.commit()
    conn.close()

    settlement_data = {
        "settlement_id": settlement_id,
        "date": today_str,
        "total_clients": total_clients,
        "app_clients": app_clients,
        "walkin_clients": walkin_clients,
        "gross_services_revenue": gross_services_revenue,
        "products_sold_count": products_sold_count,
        "products_sold_revenue": products_sold_revenue,
        "barber_product_commission": barber_commission_earned,
        "total_barber_take_home": total_barber_take_home,
        "no_show_strikes": no_shows,
    }

    broadcaster.broadcast("salon_closed", {"salon_id": salon_id, "settlement": settlement_data})
    return jsonify({"success": True, "settlement": settlement_data})


@app.route("/api/barber/open-day", methods=["POST"])
def open_day():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    conn = get_db_connection()
    conn.execute("UPDATE salons SET status = 'open' WHERE id = ?", (salon_id,))
    conn.commit()
    conn.close()

    broadcaster.broadcast("salon_opened", {"salon_id": salon_id})
    return jsonify({"success": True, "message": "تم فتح الصالون واستقبال الزبائن بنجاح"})


# -------------------------------------------------------------
# REAL-TIME SSE STREAM
# -------------------------------------------------------------
@app.route("/api/stream")
def sse_stream():
    def event_stream():
        q = broadcaster.listen()
        yield f"data: {json.dumps({'event': 'connected', 'timestamp': time.time()})}\n\n"
        try:
            import queue as py_queue
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except py_queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            broadcaster.remove(q)

    res = Response(event_stream(), mimetype="text/event-stream")
    res.headers["Cache-Control"] = "no-cache, no-transform"
    res.headers["X-Accel-Buffering"] = "no"
    res.headers["Connection"] = "keep-alive"
    return res


# -------------------------------------------------------------
# FREELANCE MOBILE BARBER API ENDPOINTS (BRICORAM / YASSIR STYLE)
# -------------------------------------------------------------

@app.route("/api/freelance/barbers", methods=["GET"])
def get_freelance_barbers():
    conn = get_db_connection()
    wilaya = request.args.get("wilaya")
    commune = request.args.get("commune")
    query = "SELECT * FROM freelance_barbers WHERE 1=1"
    params = []
    if wilaya:
        query += " AND wilaya LIKE ?"
        params.append(f"%{wilaya}%")
    if commune:
        query += " AND (commune LIKE ? OR working_zones LIKE ?)"
        params.extend([f"%{commune}%", f"%{commune}%"])
    query += " ORDER BY is_online DESC, rating DESC"
    barbers = conn.execute(query, params).fetchall()
    result = []
    for b in barbers:
        b_dict = dict(b)
        if b_dict.get("services_offered_json"):
            try:
                b_dict["services"] = json.loads(b_dict["services_offered_json"])
            except Exception:
                b_dict["services"] = []
        result.append(b_dict)
    conn.close()
    return jsonify({"barbers": result})


@app.route("/api/freelance/barber/<barber_id>", methods=["GET"])
def get_freelance_barber_profile(barber_id):
    conn = get_db_connection()
    barber = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    if not barber:
        conn.close()
        return jsonify({"error": "Barber not found"}), 404
    b_dict = dict(barber)
    if b_dict.get("services_offered_json"):
        try:
            b_dict["services"] = json.loads(b_dict["services_offered_json"])
        except Exception:
            b_dict["services"] = []
    requests_raw = conn.execute("SELECT * FROM home_service_requests WHERE barber_id = ? ORDER BY created_at DESC", (barber_id,)).fetchall()
    b_dict["requests"] = [dict(r) for r in requests_raw]
    conn.close()
    return jsonify({"barber": b_dict})


@app.route("/login")
def unified_login():
    conn = get_db_connection()
    salons = conn.execute("SELECT id, name, barber_name FROM salons").fetchall()
    barbers = conn.execute("SELECT id, name, phone FROM freelance_barbers").fetchall()
    conn.close()
    role = request.args.get("role", "freelance")
    target = request.args.get("target", "")
    error = request.args.get("error", "")
    return render_template("login.html", salons=[dict(s) for s in salons], barbers=[dict(b) for b in barbers], role=role, target=target, error=error)


@app.route("/api/freelance/login", methods=["POST"])
def freelance_login():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    barber_id = data.get("barber_id", "").strip()
    pin_code = str(data.get("pin_code") or "").strip()

    conn = get_db_connection()
    if barber_id:
        barber = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    elif phone:
        barber = conn.execute("SELECT * FROM freelance_barbers WHERE phone = ?", (phone,)).fetchone()
    else:
        conn.close()
        return jsonify({"error": "يرجى إدخال رقم الهاتف أو المعرف"}), 400

    if not barber:
        conn.close()
        return jsonify({"error": "الحساب غير مسجل في المنصة. يمكنك التسجيل كحلاق جديد."}), 404

    # Verify PIN (Default PIN: 1234)
    expected_pin = barber["pin_code"] or "1234"
    if pin_code and pin_code != expected_pin:
        conn.close()
        return jsonify({"error": "رمز PIN السري غير صحيح. الرمز الافتراضي هو 1234"}), 401

    b_dict = dict(barber)
    target_id = b_dict["id"]
    conn.close()

    resp = jsonify({"success": True, "barber": b_dict, "redirect": f"/freelance/{target_id}"})
    resp.set_cookie("freelance_auth_token", f"free_{target_id}", max_age=30*86400, httponly=True)
    return resp


@app.route("/api/freelance/logout", methods=["POST", "GET"])
def freelance_logout():
    resp = redirect("/login?role=freelance") if request.method == "GET" else jsonify({"success": True, "message": "تم تسجيل الخروج"})
    resp.delete_cookie("freelance_auth_token")
    return resp


@app.route("/api/salon/login", methods=["POST"])
def salon_login():
    data = request.json or {}
    salon_id = (data.get("salon_id") or "").strip()
    phone = (data.get("phone") or "").strip()
    pin_code = str(data.get("pin_code") or "").strip()

    conn = get_db_connection()
    salon = None
    if phone:
        clean_phone = phone.replace(" ", "").replace("-", "")
        salon = conn.execute("SELECT * FROM salons WHERE phone = ? OR phone LIKE ?", 
                             (clean_phone, f"%{clean_phone[-9:]}")).fetchone()
    if not salon and salon_id:
        salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()

    if not salon:
        conn.close()
        return jsonify({"error": "الصالون غير مسجل. يرجى التحقق من رقم الهاتف أو تسجيل صالون جديد."}), 404

    expected_pin = str(salon["pin_code"] or "1234")
    if pin_code and pin_code != expected_pin:
        conn.close()
        return jsonify({"error": "رمز PIN للصالون غير صحيح. الرمز الافتراضي: 1234"}), 401

    sid = salon["id"]
    conn.close()
    resp = jsonify({
        "success": True, 
        "redirect": f"/barber/{sid}",
        "salon_id": sid,
        "salon_name": salon["name"],
        "message": f"مرحباً بك مجدداً في {salon['name']}!"
    })
    resp.set_cookie("salon_auth_token", f"salon_{sid}", max_age=30*86400, httponly=True)
    resp.set_cookie("barber_last_salon", sid, max_age=30*86400)
    return resp


@app.route("/api/salon/register", methods=["POST"])
def salon_register():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    barber_name = (data.get("barber_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    wilaya = (data.get("wilaya") or "").strip()
    commune = (data.get("commune") or "").strip()
    pin_code = str(data.get("pin_code") or "1234").strip()
    chairs_count = int(data.get("chairs_count") or 1)

    if not name:
        return jsonify({"error": "يرجى كتابة اسم الصالون"}), 400
    if not barber_name:
        return jsonify({"error": "يرجى كتابة اسم صاحب الصالون أو الحلاق المسؤول"}), 400
    if not phone or len(phone) < 9:
        return jsonify({"error": "يرجى إدخال رقم هاتف صحيح (05/06/07)"}), 400
    if not wilaya:
        return jsonify({"error": "يرجى اختيار الولاية"}), 400

    conn = get_db_connection()
    clean_phone = phone.replace(" ", "").replace("-", "")
    existing = conn.execute("SELECT id, name FROM salons WHERE phone = ?", (clean_phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"رقم الهاتف {phone} مسجل مسبقاً لـ '{existing['name']}'. يرجى تسجيل الدخول مباشرة."}), 400

    import random, string, time
    clean_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    salon_id = f"salon_{int(time.time()) % 10000}_{clean_suffix}"

    conn.execute("""
        INSERT INTO salons (id, name, barber_name, phone, wilaya, commune, status, pin_code, is_eid_mode, active_chair_id)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, 0, 1)
    """, (salon_id, name, barber_name, clean_phone, wilaya, commune or "وسط المدينة", pin_code or "1234"))

    # Add starter chairs
    conn.execute("""
        INSERT INTO salon_chairs (salon_id, chair_index, barber_name, status, commission_rate)
        VALUES (?, 1, ?, 'active', 50.0)
    """, (salon_id, barber_name))
    if chairs_count >= 2:
        for c in range(2, chairs_count + 1):
            conn.execute("""
                INSERT INTO salon_chairs (salon_id, chair_index, barber_name, status, commission_rate)
                VALUES (?, ?, ?, 'active', 50.0)
            """, (salon_id, c, f"حلاق كرسي {c}"))

    # Add starter services
    starter_services = [
        (f"srv_{salon_id}_1", salon_id, "قص شعر كلاسيكي / عصري", 25, 500.0, "✂️", 1),
        (f"srv_{salon_id}_2", salon_id, "تحديد ورعاية اللحية بالبخار", 15, 400.0, "🧔", 1),
        (f"srv_{salon_id}_3", salon_id, "باقة VIP شاملة (شعر + لحية + سشوار)", 45, 1200.0, "👑", 1),
    ]
    for srv in starter_services:
        conn.execute("""
            INSERT INTO services (id, salon_id, name, duration_mins, price, icon, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, srv)

    conn.commit()
    conn.close()

    resp = jsonify({
        "success": True,
        "salon_id": salon_id,
        "redirect": f"/barber/{salon_id}",
        "message": f"🎉 مبروك! تم تسجيل صالون '{name}' بنجاح!"
    })
    resp.set_cookie("salon_auth_token", f"salon_{salon_id}", max_age=30*86400, httponly=True)
    resp.set_cookie("barber_last_salon", salon_id, max_age=30*86400)
    return resp


@app.route("/api/salon/logout", methods=["POST", "GET"])
def salon_logout():
    resp = redirect("/login?role=salon") if request.method == "GET" else jsonify({"success": True, "message": "تم تسجيل الخروج"})
    resp.delete_cookie("salon_auth_token")
    return resp


@app.route("/api/freelance/register", methods=["POST"])
def freelance_register():
    data = request.json or {}
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    wilaya = data.get("wilaya", "الجزائر العاصمة (16)").strip()
    commune = data.get("commune", "").strip()
    working_zones = data.get("working_zones", "").strip()
    vehicle_type = data.get("vehicle_type", "moto").strip()
    bio = data.get("bio", "").strip()
    base_price = float(data.get("base_price", 1200))
    baridimob_rip = data.get("baridimob_rip", "00799999000123456789").strip()
    pin_code = str(data.get("pin_code") or "1234").strip()
    id_card_number = data.get("id_card_number", "").strip()

    if not name or not phone or not commune:
        return jsonify({"error": "الاسم ورقم الهاتف والبلدية حقول إجبارية"}), 400

    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM freelance_barbers WHERE phone = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "رقم الهاتف مسجل مسبقاً، يمكنك تسجيل الدخول به مباشرة"}), 400

    barber_id = f"barber_free_{uuid.uuid4().hex[:8]}"
    default_services = json.dumps([
        {"id": f"srv_{uuid.uuid4().hex[:4]}", "name": "حلاقة شعر احترافية (Fade / كلاسيك)", "price": base_price, "duration": 30},
        {"id": f"srv_{uuid.uuid4().hex[:4]}", "name": "تحديد لحية ملكي بالمنشفة الساخنة", "price": int(base_price * 0.6), "duration": 20},
        {"id": f"srv_{uuid.uuid4().hex[:4]}", "name": "باقة VIP متكاملة (شعر + لحية + عناية)", "price": int(base_price * 1.5), "duration": 50}
    ], ensure_ascii=False)

    # By default: newly registered barbers require KYC approval (is_verified = 0, kyc_status = 'pending' or 'unsubmitted')
    kyc_status = "pending" if id_card_number else "unsubmitted"
    conn.execute("""
        INSERT INTO freelance_barbers 
        (id, name, phone, wilaya, commune, working_zones, vehicle_type, bio, rating, reviews_count, completed_jobs, is_online, is_verified, base_price, services_offered_json, baridimob_rip, pin_code, id_card_number, kyc_status, wallet_balance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 5.0, 1, 0, 0, 0, ?, ?, ?, ?, ?, ?, 2000.0)
    """, (barber_id, name, phone, wilaya, commune, working_zones or commune, vehicle_type, bio, base_price, default_services, baridimob_rip, pin_code, id_card_number, kyc_status))
    conn.commit()
    new_barber = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    conn.close()

    broadcaster.broadcast("freelance_registered", {"barber_id": barber_id, "name": name, "commune": commune, "kyc_status": kyc_status})
    resp = jsonify({"success": True, "barber": dict(new_barber), "redirect": f"/freelance/{barber_id}"})
    resp.set_cookie("freelance_auth_token", f"free_{barber_id}", max_age=30*86400, httponly=True)
    return resp


@app.route("/api/freelance/kyc/submit", methods=["POST"])
def freelance_kyc_submit():
    data = request.json or {}
    barber_id = data.get("barber_id")
    id_card_number = data.get("id_card_number", "").strip()
    id_card_front = data.get("id_card_front", "").strip()
    id_card_back = data.get("id_card_back", "").strip()
    selfie_photo = data.get("selfie_photo", "").strip()
    equipment_photo = data.get("equipment_photo", "").strip()

    if not barber_id or not id_card_number:
        return jsonify({"error": "رقم بطاقة التعريف الوطنية البيومترية إجباري"}), 400

    conn = get_db_connection()
    conn.execute("""
        UPDATE freelance_barbers
        SET id_card_number = ?,
            id_card_front = COALESCE(NULLIF(?, ''), id_card_front),
            id_card_back = COALESCE(NULLIF(?, ''), id_card_back),
            selfie_photo = COALESCE(NULLIF(?, ''), selfie_photo),
            equipment_photo = COALESCE(NULLIF(?, ''), equipment_photo),
            kyc_status = 'pending',
            is_verified = 0,
            kyc_submitted_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (id_card_number, id_card_front, id_card_back, selfie_photo, equipment_photo, barber_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("kyc_submitted", {
        "barber_id": barber_id,
        "id_card_number": id_card_number,
        "status": "pending"
    })
    return jsonify({"success": True, "message": "تم تقديم وثائق الهوية وصورة العتاد بنجاح! سيتم فحصها وتفعيل الحساب خلال دقائق."})


@app.route("/api/admin/kyc/review", methods=["POST"])
def admin_kyc_review():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    barber_id = data.get("barber_id")
    action = data.get("action")  # 'approve' or 'reject'
    reason = data.get("reason", "").strip()

    if not barber_id or action not in ["approve", "reject"]:
        return jsonify({"error": "barber_id and valid action ('approve' or 'reject') required"}), 400

    conn = get_db_connection()
    if action == "approve":
        conn.execute("""
            UPDATE freelance_barbers
            SET kyc_status = 'approved',
                is_verified = 1,
                kyc_rejection_reason = '',
                kyc_reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (barber_id,))
        status_msg = "تم توثيق واعتماد الحلاق بنجاح 🛡️"
    else:
        conn.execute("""
            UPDATE freelance_barbers
            SET kyc_status = 'rejected',
                is_verified = 0,
                is_online = 0,
                kyc_rejection_reason = ?,
                kyc_reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (reason or "وثائق الهوية غير واضحة", barber_id))
        status_msg = "تم رفض طلب التوثيق"

    conn.commit()
    conn.close()

    broadcaster.broadcast("kyc_reviewed", {
        "barber_id": barber_id,
        "action": action,
        "kyc_status": "approved" if action == "approve" else "rejected"
    })
    return jsonify({"success": True, "message": status_msg})


@app.route("/api/freelance/toggle-status", methods=["POST"])
def freelance_toggle_status():
    data = request.json or {}
    barber_id = data.get("barber_id")
    if not barber_id:
        return jsonify({"error": "barber_id required"}), 400
    conn = get_db_connection()
    barber = conn.execute("SELECT is_online, is_verified, kyc_status, wallet_balance FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    if not barber:
        conn.close()
        return jsonify({"error": "Barber not found"}), 404

    current_status = barber["is_online"]
    new_status = 0 if current_status == 1 else 1

    # 🚫 Security Enforcement: Cannot go online if not verified / KYC not approved!
    if new_status == 1 and (barber["is_verified"] != 1 or barber["kyc_status"] != "approved"):
        conn.close()
        return jsonify({
            "error": "عذراً! لا يمكنك تفعيل استقبال الطلبات حتى يتم اعتماد وتوثيق بطاقة تعريفك الوطنية من طرف الإدارة 🛡️"
        }), 403

    # 💳 Financial Protection: Cannot go online if wallet balance < 300 DZD!
    wallet_bal = float(barber["wallet_balance"] or 0.0)
    if new_status == 1 and wallet_bal < 300.0:
        conn.close()
        return jsonify({
            "error": f"عذراً! رصيد محفظتك ({int(wallet_bal)} د.ج) أقل من 300 د.ج المطلوبة لتغطية عمولة المنصة 15%. يرجى شحن محفظتك ببريدي موب لبدء استقبال الطلبات 💳"
        }), 403

    conn.execute("UPDATE freelance_barbers SET is_online = ? WHERE id = ?", (new_status, barber_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("freelance_status_changed", {"barber_id": barber_id, "is_online": new_status})
    return jsonify({"success": True, "is_online": new_status})


@app.route("/api/freelance/request/action", methods=["POST"])
def freelance_request_action():
    data = request.json or {}
    request_id = data.get("request_id")
    action = data.get("action")
    barber_id = data.get("barber_id")

    if not request_id or not action:
        return jsonify({"error": "request_id and action required"}), 400

    conn = get_db_connection()
    req = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        return jsonify({"error": "Request not found"}), 404

    status_map = {
        "accept": "accepted",
        "on_the_way": "on_the_way",
        "complete": "completed",
        "reject": "cancelled",
        "cancel": "cancelled"
    }
    new_status = status_map.get(action, "accepted")

    target_barber_id = barber_id or req["barber_id"]

    if action == "accept" and target_barber_id:
        conn.execute("UPDATE home_service_requests SET status = ?, barber_id = ? WHERE id = ?", (new_status, target_barber_id, request_id))
    elif action == "complete":
        conn.execute("UPDATE home_service_requests SET status = ? WHERE id = ?", (new_status, request_id))
        if target_barber_id:
            conn.execute("UPDATE freelance_barbers SET completed_jobs = completed_jobs + 1 WHERE id = ?", (target_barber_id,))
    else:
        conn.execute("UPDATE home_service_requests SET status = ? WHERE id = ?", (new_status, request_id))

    conn.commit()
    updated = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (request_id,)).fetchone()

    barber_data = None
    final_barber_id = updated["barber_id"]
    if final_barber_id:
        b_row = conn.execute("SELECT id, name, phone, vehicle_type, rating, commune FROM freelance_barbers WHERE id = ?", (final_barber_id,)).fetchone()
        if b_row:
            barber_data = dict(b_row)

    conn.close()

    broadcaster.broadcast("home_request_updated", {
        "request_id": request_id, 
        "status": new_status, 
        "barber_id": final_barber_id,
        "barber": barber_data
    })
    return jsonify({"success": True, "status": new_status, "request": dict(updated), "barber": barber_data})


@app.route("/api/freelance/request/<request_id>", methods=["GET"])
def get_home_service_request(request_id):
    conn = get_db_connection()
    req = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        return jsonify({"error": "Request not found"}), 404
    req_dict = dict(req)
    barber_dict = None
    if req_dict.get("barber_id"):
        b_row = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (req_dict["barber_id"],)).fetchone()
        if b_row:
            barber_dict = dict(b_row)
    conn.close()
    return jsonify({"success": True, "request": req_dict, "barber": barber_dict})


@app.route("/api/freelance/request/rate", methods=["POST"])
def rate_home_service_request():
    data = request.json or {}
    request_id = data.get("request_id")
    rating = int(data.get("rating", 5))
    review = data.get("review", "").strip()

    if not request_id:
        return jsonify({"error": "request_id required"}), 400

    rating = max(1, min(5, rating))

    conn = get_db_connection()
    req = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        return jsonify({"error": "Request not found"}), 404

    conn.execute("UPDATE home_service_requests SET customer_rating = ?, customer_review = ? WHERE id = ?", (rating, review, request_id))
    conn.commit()

    barber_id = req["barber_id"]
    new_avg = 4.95
    if barber_id:
        avg_row = conn.execute("SELECT AVG(customer_rating), COUNT(*) FROM home_service_requests WHERE barber_id = ? AND customer_rating > 0", (barber_id,)).fetchone()
        if avg_row and avg_row[1] > 0:
            new_avg = round(float(avg_row[0]), 2)
            conn.execute("UPDATE freelance_barbers SET rating = ?, reviews_count = reviews_count + 1 WHERE id = ?", (new_avg, barber_id))
            conn.commit()

    conn.close()
    broadcaster.broadcast("home_request_rated", {"request_id": request_id, "rating": rating, "barber_id": barber_id})
    return jsonify({"success": True, "rating": rating, "new_barber_rating": new_avg})


@app.route("/api/freelance/request/create", methods=["POST"])
def create_home_service_request():
    data = request.json or {}
    customer_name = data.get("customer_name", "").strip()
    customer_phone = data.get("customer_phone", "").strip()
    wilaya = data.get("wilaya", "الجزائر العاصمة (16)").strip()
    commune = data.get("commune", "").strip()
    address_details = data.get("address_details", "").strip()
    service_type = data.get("service_type", "حلاقة منزلية شاملة").strip()
    price = float(data.get("price", 1500))
    barber_id = data.get("barber_id")
    scheduled_at = data.get("scheduled_at", "الآن (فوري)").strip()
    notes = data.get("notes", "").strip()
    payment_method = data.get("payment_method", "cash").strip()

    if not customer_name or not customer_phone or not commune or not address_details:
        return jsonify({"error": "الاسم، الهاتف، البلدية، وتفاصيل العنوان حقول إجبارية"}), 400

    req_id = f"req_home_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO home_service_requests 
        (id, customer_name, customer_phone, wilaya, commune, address_details, service_type, price, barber_id, status, scheduled_at, notes, payment_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
    """, (req_id, customer_name, customer_phone, wilaya, commune, address_details, service_type, price, barber_id, scheduled_at, notes, payment_method))
    conn.commit()
    created = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (req_id,)).fetchone()
    conn.close()

    broadcaster.broadcast("home_request_created", {"request_id": req_id, "commune": commune, "service_type": service_type})
    return jsonify({"success": True, "request_id": req_id, "request": dict(created)})


# -------------------------------------------------------------
# WEB & PWA ROUTES
# -------------------------------------------------------------

@app.route("/portal")
def portal_role_select():
    return render_template("portal.html")


@app.route("/freelance")
@app.route("/freelance/<barber_id>")
def freelance_dashboard(barber_id=None):
    conn = get_db_connection()
    auth_cookie = request.cookies.get("freelance_auth_token")
    is_admin = (request.cookies.get("admin_auth_token") == "master_admin_session_valid")

    if not barber_id:
        if auth_cookie and auth_cookie.startswith("free_"):
            barber_id = auth_cookie[5:]
        else:
            barber_id = "barber_free_1"

    barber = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    if not barber:
        conn.close()
        return redirect("/portal")

    # 🔐 IDOR Protection & Session Check
    if not is_admin and auth_cookie != f"free_{barber_id}":
        conn.close()
        return redirect(f"/login?role=freelance&target={barber_id}&error=unauthorized")

    barber_dict = dict(barber)
    services_list = []
    if barber_dict.get("services_offered_json"):
        try:
            services_list = json.loads(barber_dict["services_offered_json"])
        except Exception:
            services_list = []

    # Get requests assigned to this barber or pending
    requests_raw = conn.execute("""
        SELECT * FROM home_service_requests 
        WHERE barber_id = ? OR (barber_id IS NULL AND status = 'pending')
        ORDER BY CASE 
            WHEN status = 'on_the_way' THEN 1
            WHEN status = 'accepted' THEN 2
            WHEN status = 'pending' THEN 3
            ELSE 4 END, created_at DESC
    """, (barber_id,)).fetchall()

    requests_list = [dict(r) for r in requests_raw]

    # Calculate wallet from completed home requests
    completed_stats = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(price), 0.0) 
        FROM home_service_requests 
        WHERE barber_id = ? AND status = 'completed'
    """, (barber_id,)).fetchone()
    gross_earnings = completed_stats[1]
    platform_commission = gross_earnings * 0.10
    net_earnings = gross_earnings * 0.90
    wallet = {
        "completed_count": completed_stats[0],
        "gross_earnings": gross_earnings,
        "platform_commission": platform_commission,
        "net_earnings": net_earnings
    }

    conn.close()
    return render_template("freelance_dashboard.html", barber=barber_dict, services_list=services_list, requests=requests_list, wallet=wallet)


@app.route("/freelance/request")
def freelance_request_page():
    conn = get_db_connection()
    barbers = conn.execute("SELECT * FROM freelance_barbers WHERE is_online = 1 ORDER BY rating DESC").fetchall()
    barbers_list = []
    for b in barbers:
        b_dict = dict(b)
        if b_dict.get("services_offered_json"):
            try:
                b_dict["services"] = json.loads(b_dict["services_offered_json"])
            except Exception:
                b_dict["services"] = []
        barbers_list.append(b_dict)
    conn.close()
    return render_template("freelance_request.html", barbers=barbers_list, local_ip=get_local_ip())


@app.route("/freelance/track/<request_id>")
def freelance_track_page(request_id):
    conn = get_db_connection()
    req = conn.execute("SELECT * FROM home_service_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        return redirect("/freelance/request")
    
    req_dict = dict(req)
    barber_dict = None
    if req_dict.get("barber_id"):
        b_row = conn.execute("SELECT * FROM freelance_barbers WHERE id = ?", (req_dict["barber_id"],)).fetchone()
        if b_row:
            barber_dict = dict(b_row)

    conn.close()
    return render_template("freelance_track.html", req=req_dict, barber=barber_dict)


@app.route("/")
@app.route("/explore")
def marketplace_home():
    conn = get_db_connection()
    salons_raw = conn.execute("SELECT * FROM salons WHERE status = 'open'").fetchall()
    salons_list = []
    for s in salons_raw:
        s_dict = dict(s)
        waiting_count = conn.execute(
            "SELECT count(*) FROM queue_tickets WHERE salon_id = ? AND status IN ('waiting', 'turn_notified', 'arrived')",
            (s['id'],)
        ).fetchone()[0]
        services = conn.execute(
            "SELECT name, price FROM services WHERE salon_id = ? AND is_active = 1 LIMIT 3",
            (s['id'],)
        ).fetchall()
        s_dict['waiting_count'] = waiting_count
        s_dict['est_wait_minutes'] = waiting_count * 18
        s_dict['services'] = [dict(srv) for srv in services]

        # Parse Amenities
        amenities_raw = s_dict.get('amenities')
        if amenities_raw:
            try:
                s_dict['amenities_list'] = json.loads(amenities_raw) if isinstance(amenities_raw, str) else amenities_raw
            except Exception:
                s_dict['amenities_list'] = ['clima', 'baridimob', 'coffee', 'parking', 'sanitized']
        else:
            s_dict['amenities_list'] = ['clima', 'baridimob', 'coffee', 'parking', 'sanitized']

        # Google Maps search URL
        maps_target = s_dict.get('maps_query') or f"{s_dict.get('name', '')} {s_dict.get('commune', '')} {s_dict.get('wilaya', '')}"
        import urllib.parse
        s_dict['maps_url'] = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(maps_target)}"

        # Reviews fallback
        s_dict['rating'] = s_dict.get('rating') or 4.9
        s_dict['reviews_count'] = s_dict.get('reviews_count') or 85

        salons_list.append(s_dict)
    conn.close()
    return render_template("explore.html", salons=salons_list, local_ip=get_local_ip())


@app.route("/tv")
@app.route("/tv/<salon_id>")
def tv_waiting_screen(salon_id=None):
    conn = get_db_connection()
    all_salons = conn.execute("SELECT id, name, barber_name, wilaya, commune FROM salons").fetchall()
    all_salons_list = [dict(s) for s in all_salons]

    if not salon_id:
        salon_id = all_salons_list[0]["id"] if all_salons_list else "salon_dz_1"

    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    admin_phone_row = conn.execute("SELECT value FROM admin_settings WHERE key = 'admin_phone'").fetchone()
    admin_phone = admin_phone_row["value"] if admin_phone_row else "0555 12 34 56"
    conn.close()

    if not salon:
        salon = all_salons_list[0] if all_salons_list else {"id": salon_id, "name": "صالون الأناقة الملكي", "barber_name": "المعلم رشيد", "wilaya": "الجزائر العاصمة (16)", "commune": "باب الزوار"}

    salon_dict = dict(salon)

    # 📺 Check if TV Mode is activated exclusively by Admin
    if not salon_dict.get("enable_tv_mode"):
        return render_template("tv_locked.html", salon=salon_dict, admin_phone=admin_phone)

    return render_template("tv_screen.html", salon_id=salon_id, salon=salon_dict, all_salons=all_salons_list, local_ip=get_local_ip())


@app.route("/salon")
@app.route("/salon/<salon_id>")
def customer_portal(salon_id=None):
    conn = get_db_connection()
    all_salons = conn.execute("SELECT id, name, barber_name, wilaya, commune FROM salons").fetchall()
    all_salons_list = [dict(s) for s in all_salons]

    if not salon_id:
        salon_id = all_salons_list[0]["id"] if all_salons_list else "salon_dz_1"

    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    conn.close()
    if not salon:
        salon = all_salons_list[0] if all_salons_list else {"id": salon_id, "name": "صالون الأناقة الملكي", "barber_name": "المعلم رشيد", "wilaya": "الجزائر العاصمة (16)", "commune": "باب الزوار"}

    salon_dict = dict(salon)
    is_suspended = (salon_dict.get("subscription_status") == "suspended")
    return render_template("customer.html", salon_id=salon_id, salon=salon_dict, all_salons=all_salons_list, local_ip=get_local_ip(), is_suspended=is_suspended)


@app.route("/barber")
@app.route("/barber/<salon_id>")
def barber_dashboard(salon_id=None):
    conn = get_db_connection()
    all_salons = conn.execute("SELECT id, name, barber_name, wilaya, commune FROM salons").fetchall()
    all_salons_list = [dict(s) for s in all_salons]

    if not salon_id:
        cookie_salon = request.cookies.get("barber_last_salon")
        if cookie_salon and any(s["id"] == cookie_salon for s in all_salons_list):
            conn.close()
            return redirect(f"/barber/{cookie_salon}")
        salon_id = all_salons_list[0]["id"] if all_salons_list else "salon_dz_1"

    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    
    # 🚫 Check if salon subscription is suspended by Admin
    if salon and salon["subscription_status"] == "suspended":
        admin_settings_rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
        settings_map = {r["key"]: r["value"] for r in admin_settings_rows}
        conn.close()
        return render_template("salon_suspended.html", salon=dict(salon), admin_settings=settings_map)

    conn.close()
    if not salon:
        salon = all_salons_list[0] if all_salons_list else {"id": salon_id, "name": "صالون الأناقة الملكي", "barber_name": "المعلم رشيد", "wilaya": "الجزائر العاصمة (16)", "commune": "باب الزوار"}

    resp = make_response(render_template("barber.html", salon_id=salon_id, salon=dict(salon), all_salons=all_salons_list, local_ip=get_local_ip()))
    resp.set_cookie("barber_last_salon", salon_id, max_age=30*86400)
    return resp


# -------------------------------------------------------------
# 🖥️ DESKTOP CAISSE & POS MANAGEMENT SCREEN (شاشة لاكيس المكتبي)
# -------------------------------------------------------------
@app.route("/caisse")
@app.route("/caisse/<salon_id>")
def caisse_pos_dashboard(salon_id=None):
    conn = get_db_connection()
    all_salons = conn.execute("SELECT id, name, barber_name, wilaya, commune FROM salons").fetchall()
    all_salons_list = [dict(s) for s in all_salons]

    if not salon_id:
        cookie_salon = request.cookies.get("caisse_last_salon") or request.cookies.get("barber_last_salon")
        if cookie_salon and any(s["id"] == cookie_salon for s in all_salons_list):
            conn.close()
            return redirect(f"/caisse/{cookie_salon}")
        salon_id = all_salons_list[0]["id"] if all_salons_list else "salon_dz_1"

    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    
    # 🚫 Check if salon subscription is suspended by Admin
    if salon and salon["subscription_status"] == "suspended":
        admin_settings_rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
        settings_map = {r["key"]: r["value"] for r in admin_settings_rows}
        conn.close()
        return render_template("salon_suspended.html", salon=dict(salon), admin_settings=settings_map)

    # Fetch b2c products for counter sales
    products_rows = conn.execute("SELECT * FROM products WHERE type = 'b2c_hero'").fetchall()
    products_list = [dict(p) for p in products_rows]

    conn.close()
    if not salon:
        salon = all_salons_list[0] if all_salons_list else {"id": salon_id, "name": "صالون الأناقة الملكي", "barber_name": "المعلم رشيد", "wilaya": "الجزائر العاصمة (16)", "commune": "باب الزوار"}

    resp = make_response(render_template(
        "caisse.html", 
        salon_id=salon_id, 
        salon=dict(salon), 
        all_salons=all_salons_list, 
        b2c_products=products_list,
        local_ip=get_local_ip()
    ))
    resp.set_cookie("caisse_last_salon", salon_id, max_age=30*86400)
    return resp


@app.route("/api/caisse/direct-sale", methods=["POST"])
def caisse_direct_sale():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    product_id = data.get("product_id")
    qty = int(data.get("quantity", 1))
    payment_method = data.get("payment_method", "cash") # 'cash' or 'baridimob'

    if not product_id or qty <= 0:
        return jsonify({"error": "المنتج والكمية مطلوبان"}), 400

    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "المنتج غير موجود"}), 404

    unit_price = float(product["price"])
    total_price = unit_price * qty

    # Record ticket as completed direct sale
    ticket_id = f"caisse_{uuid.uuid4().hex[:6]}"
    ordered_products = [{
        "id": product["id"],
        "name": product["name"],
        "price": unit_price,
        "quantity": qty,
        "icon": product["icon"] or "✨"
    }]

    conn.execute("""
        INSERT INTO queue_tickets (
            id, salon_id, customer_name, customer_phone, services_json,
            total_duration, total_service_price, status, is_walkin,
            ordered_products_json, completed_at, payment_method
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', 1, ?, CURRENT_TIMESTAMP, ?)
    """, (
        ticket_id, salon_id, f"بيع مباشر ({product['name']})", "0000000000",
        json.dumps([{"name": f"بيع {product['name']} x{qty}", "price": total_price}], ensure_ascii=False),
        0, total_price, json.dumps(ordered_products, ensure_ascii=False), payment_method
    ))

    # Decrement stock
    conn.execute("UPDATE products SET stock_count = MAX(0, stock_count - ?) WHERE id = ?", (qty, product["id"]))
    conn.commit()
    conn.close()

    broadcaster.broadcast("chairs_updated", {"salon_id": salon_id, "action": "counter_sale"})
    broadcaster.broadcast("queue_updated", {"salon_id": salon_id, "action": "counter_sale"})

    return jsonify({
        "success": True,
        "ticket_id": ticket_id,
        "total_price": total_price,
        "message": f"تم تسجيل بيع {product['name']} ({total_price:,.0f} د.ج) بنجاح في صندوق لاكيس!"
    })


@app.route("/api/salons", methods=["GET"])
def get_all_salons():
    conn = get_db_connection()
    salons = conn.execute("SELECT * FROM salons").fetchall()
    conn.close()
    return jsonify({"salons": [dict(s) for s in salons]})


@app.route("/api/salons/add", methods=["POST"])
def add_new_salon():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    barber_name = data.get("barber_name", "").strip()
    wilaya = data.get("wilaya", "").strip()
    commune = data.get("commune", "").strip()
    phone = data.get("phone", "0550000000").strip()

    if not name or not barber_name or not wilaya:
        return jsonify({"error": "الرجاء إدخال اسم الصالون، الحلاق المسؤول، والولاية"}), 400

    import random, string, time
    clean_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    base_id = f"salon_{int(time.time()) % 10000}_{clean_suffix}"

    conn = get_db_connection()
    conn.execute("""
        INSERT INTO salons (id, name, barber_name, phone, wilaya, commune, status, is_eid_mode, active_chair_id)
        VALUES (?, ?, ?, ?, ?, ?, 'open', 0, 1)
    """, (base_id, name, barber_name, phone, wilaya, commune))

    # Add standard starter services
    services_data = [
        (f"srv_{base_id}_1", base_id, "قص شعر كلاسيكي / عصري", 25, 500.0, "✂️", 1),
        (f"srv_{base_id}_2", base_id, "تحديد ورعاية اللحية بالبخار", 15, 400.0, "🧔", 1),
        (f"srv_{base_id}_3", base_id, "باقة VIP المتكاملة (شعر + لحية + غسيل)", 40, 900.0, "👑", 1),
        (f"srv_{base_id}_4", base_id, "تنظيف بشرة وماسك الفحم", 20, 600.0, "✨", 1),
        (f"srv_{base_id}_5", base_id, "كيراتين وعلاج بروتين", 60, 3500.0, "🧴", 1)
    ]
    for srv in services_data:
        conn.execute("INSERT OR REPLACE INTO services (id, salon_id, name, duration_minutes, price, icon, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)", srv)

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "salon_id": base_id,
        "message": f"تمت إضافة {name} بنجاح!"
    })


@app.route("/api/salons/<salon_id>", methods=["DELETE"])
def delete_salon(salon_id):
    if salon_id == "salon_dz_1":
        return jsonify({"error": "لا يمكن حذف الصالون الرئيسي للجزائر العاصمة"}), 400
    conn = get_db_connection()
    conn.execute("DELETE FROM salons WHERE id = ?", (salon_id,))
    conn.execute("DELETE FROM services WHERE salon_id = ?", (salon_id,))
    conn.execute("DELETE FROM queue_tickets WHERE salon_id = ?", (salon_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم حذف الصالون بنجاح"})


@app.route("/poster")
@app.route("/salon/<salon_id>/poster")
def salon_poster(salon_id="salon_dz_1"):
    conn = get_db_connection()
    salon = conn.execute("SELECT * FROM salons WHERE id = ?", (salon_id,)).fetchone()
    all_salons = conn.execute("SELECT id, name, wilaya, commune FROM salons").fetchall()
    conn.close()
    if not salon:
        salon = all_salons[0] if all_salons else {"id": "salon_dz_1", "name": "صالون الأناقة الملكي", "barber_name": "المعلم رشيد", "phone": "0555123456", "wilaya": "الجزائر العاصمة", "commune": "باب الزوار"}
    return render_template("poster.html", salon=dict(salon), all_salons=[dict(s) for s in all_salons], local_ip=get_local_ip())


# -------------------------------------------------------------
# 11. ADMIN MODULE TOGGLE (تفعيل وإلغاء الميزات الاختيارية للصالونات)
# -------------------------------------------------------------
@app.route("/api/admin/salon/modules", methods=["POST"])
def update_salon_modules():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    salon_id = data.get("salon_id")
    if not salon_id:
        return jsonify({"error": "salon_id is required"}), 400
    
    enable_tv = 1 if data.get("enable_tv_mode") else 0
    enable_voice = 1 if data.get("enable_voice_caller") else 0
    enable_groom = 1 if data.get("enable_groom_packs") else 0
    enable_receipt = 1 if data.get("enable_receipt_print") else 0

    conn = get_db_connection()
    conn.execute("""
        UPDATE salons 
        SET enable_tv_mode = ?, enable_voice_caller = ?, enable_groom_packs = ?, enable_receipt_print = ?
        WHERE id = ?
    """, (enable_tv, enable_voice, enable_groom, enable_receipt, salon_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("salon_modules_updated", {
        "salon_id": salon_id,
        "enable_tv_mode": bool(enable_tv),
        "enable_voice_caller": bool(enable_voice),
        "enable_groom_packs": bool(enable_groom),
        "enable_receipt_print": bool(enable_receipt)
    })
    return jsonify({
        "success": True, 
        "message": "تم تحديث موديولات وميزات الصالون بنجاح!",
        "modules": {
            "enable_tv_mode": bool(enable_tv),
            "enable_voice_caller": bool(enable_voice),
            "enable_groom_packs": bool(enable_groom),
            "enable_receipt_print": bool(enable_receipt)
        }
    })


# -------------------------------------------------------------
# 12. PACK 3AROUS & VIP APPOINTMENTS (باقات العرسان والمناسبات)
# -------------------------------------------------------------
@app.route("/api/groom/book", methods=["POST"])
def book_groom_appointment():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    pack_name = (data.get("pack_name") or "باك العريس الملكي").strip()
    price = float(data.get("price") or 6000.0)
    event_date = (data.get("event_date") or "").strip()
    event_time = (data.get("event_time") or "").strip()
    acompte_paid = float(data.get("acompte_paid") or 0.0)
    notes = (data.get("notes") or "").strip()

    if not customer_name or not customer_phone or not event_date:
        return jsonify({"error": "يرجى ملء الاسم ورقم الهاتف وتاريخ المناسبة"}), 400

    appt_id = f"groom_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO groom_appointments (
            id, salon_id, customer_name, customer_phone, pack_name, price, 
            event_date, event_time, acompte_paid, notes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')
    """, (appt_id, salon_id, customer_name, customer_phone, pack_name, price, event_date, event_time, acompte_paid, notes))
    conn.commit()
    conn.close()

    broadcaster.broadcast("groom_booked", {
        "salon_id": salon_id,
        "appt_id": appt_id,
        "customer_name": customer_name,
        "pack_name": pack_name,
        "event_date": event_date,
        "event_time": event_time
    })

    return jsonify({
        "success": True,
        "appt_id": appt_id,
        "message": f"ألف مبروك يا {customer_name}! تم تثبيت حجز {pack_name} بتاريخ {event_date} بنجاح."
    })


@app.route("/api/groom/appointments/<salon_id>", methods=["GET"])
def get_groom_appointments(salon_id):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT * FROM groom_appointments 
        WHERE salon_id = ?
        ORDER BY event_date ASC, event_time ASC
    """, (salon_id,)).fetchall()
    conn.close()
    return jsonify({"appointments": [dict(r) for r in rows]})


# -------------------------------------------------------------
# 13. MONTHLY ANALYTICS & EXCEL EXPORT (التقارير والأرشيف الشهري)
# -------------------------------------------------------------
@app.route("/api/barber/analytics/monthly/<salon_id>", methods=["GET"])
def get_monthly_analytics(salon_id):
    days_back = int(request.args.get("days", 30))
    start_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d 00:00:00")

    conn = get_db_connection()
    tickets = conn.execute("""
        SELECT * FROM queue_tickets 
        WHERE salon_id = ? AND status = 'completed' AND completed_at >= ?
        ORDER BY completed_at ASC
    """, (salon_id, start_date)).fetchall()

    expenses = conn.execute("""
        SELECT * FROM salon_expenses 
        WHERE salon_id = ? AND created_at >= ?
        ORDER BY created_at ASC
    """, (salon_id, start_date)).fetchall()

    total_revenue = sum(float(t["total_service_price"]) for t in tickets)
    total_clients = len(tickets)
    total_expenses = sum(float(e["amount"]) for e in expenses)
    net_profit = max(0.0, total_revenue - total_expenses)

    # Daily aggregation
    daily_map = {}
    for t in tickets:
        d = t["completed_at"][:10] if t["completed_at"] else date.today().strftime("%Y-%m-%d")
        if d not in daily_map:
            daily_map[d] = {"date": d, "revenue": 0.0, "clients": 0, "expenses": 0.0}
        daily_map[d]["revenue"] += float(t["total_service_price"])
        daily_map[d]["clients"] += 1

    for e in expenses:
        d = e["created_at"][:10] if e["created_at"] else date.today().strftime("%Y-%m-%d")
        if d not in daily_map:
            daily_map[d] = {"date": d, "revenue": 0.0, "clients": 0, "expenses": 0.0}
        daily_map[d]["expenses"] += float(e["amount"])

    daily_list = sorted(daily_map.values(), key=lambda x: x["date"], reverse=True)
    for row in daily_list:
        row["net"] = row["revenue"] - row["expenses"]

    conn.close()
    return jsonify({
        "salon_id": salon_id,
        "period_days": days_back,
        "total_revenue": total_revenue,
        "total_clients": total_clients,
        "total_expenses": total_expenses,
        "net_profit": net_profit,
        "daily_breakdown": daily_list
    })


@app.route("/api/barber/export/csv/<salon_id>", methods=["GET"])
def export_monthly_csv(salon_id):
    conn = get_db_connection()
    salon = conn.execute("SELECT name FROM salons WHERE id = ?", (salon_id,)).fetchone()
    salon_name = salon["name"] if salon else "Salon"

    start_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    tickets = conn.execute("""
        SELECT completed_at, customer_name, customer_phone, total_service_price, preferred_barber_name, payment_method
        FROM queue_tickets 
        WHERE salon_id = ? AND status = 'completed' AND completed_at >= ?
        ORDER BY completed_at DESC
    """, (salon_id, start_date)).fetchall()

    expenses = conn.execute("""
        SELECT created_at, category, description, amount 
        FROM salon_expenses 
        WHERE salon_id = ? AND created_at >= ?
        ORDER BY created_at DESC
    """, (salon_id, start_date)).fetchall()
    conn.close()

    import io
    output = io.StringIO()
    # Excel UTF-8 BOM so Arabic renders perfectly in Excel
    output.write('\ufeff')
    output.write(f"تقرير حسابات {salon_name} لشهر {date.today().strftime('%m-%Y')}\n\n")
    output.write("--- سجل الحلاقات المكتملة ---\n")
    output.write("التاريخ,الزبون,الهاتف,المبلغ (د.ج),الحلاق,طريقة الدفع\n")
    for t in tickets:
        d = t["completed_at"][:19] if t["completed_at"] else ""
        c_name = (t["customer_name"] or "").replace(",", " ")
        c_phone = (t["customer_phone"] or "").replace(",", " ")
        price = t["total_service_price"]
        b_name = (t["preferred_barber_name"] or "المعلم").replace(",", " ")
        pay = "بريدي موب" if t["payment_method"] == "baridimob" else "كاش"
        output.write(f"{d},{c_name},{c_phone},{price},{b_name},{pay}\n")

    output.write("\n--- سجل مصاريف الصندوق (Sorties de Caisse) ---\n")
    output.write("التاريخ,النوع,البيان,المبلغ (د.ج)\n")
    for e in expenses:
        d = e["created_at"][:19] if e["created_at"] else ""
        cat = (e["category"] or "").replace(",", " ")
        desc = (e["description"] or "").replace(",", " ")
        amt = e["amount"]
        output.write(f"{d},{cat},{desc},{amt}\n")

    csv_data = output.getvalue()
    filename = f"rapport_salon_{salon_id}_{date.today().strftime('%Y%m%d')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


# -------------------------------------------------------------
# 14. CUSTOMER REVIEWS & SECRET FEEDBACK (التقييم وصندوق الشكاوى السري)
# -------------------------------------------------------------
@app.route("/api/customer/review", methods=["POST"])
def submit_customer_review():
    data = request.json or {}
    salon_id = data.get("salon_id", "salon_dz_1")
    ticket_id = (data.get("ticket_id") or "").strip()
    customer_name = (data.get("customer_name") or "زبون الصالون").strip()
    customer_phone = (data.get("customer_phone") or "").strip()
    rating = int(data.get("rating") or 5)
    comment = (data.get("comment") or "").strip()

    if rating < 1 or rating > 5:
        return jsonify({"error": "التقييم يجب أن يكون بين 1 و 5 نجوم"}), 400

    rev_id = f"rev_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO salon_reviews (id, salon_id, ticket_id, customer_name, customer_phone, rating, comment, is_secret)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (rev_id, salon_id, ticket_id, customer_name, customer_phone, rating, comment))

    # Recalculate average rating for salon
    avg_row = conn.execute("""
        SELECT AVG(rating) as avg_r, COUNT(*) as cnt FROM salon_reviews WHERE salon_id = ?
    """, (salon_id,)).fetchone()
    
    if avg_row and avg_row["cnt"] > 0:
        new_rating = round(float(avg_row["avg_r"]), 1)
        new_cnt = int(avg_row["cnt"])
        conn.execute("UPDATE salons SET rating = ?, reviews_count = ? WHERE id = ?", (new_rating, new_cnt, salon_id))

    conn.commit()
    conn.close()

    broadcaster.broadcast("review_received", {
        "salon_id": salon_id,
        "rating": rating,
        "customer_name": customer_name
    })

    return jsonify({
        "success": True,
        "message": "شكراً جزيلاً لتقييمك! رأيك وصل بأمان لإدارة الصالون لتحسين الخدمة."
    })


@app.route("/api/barber/reviews/<salon_id>", methods=["GET"])
def get_salon_reviews(salon_id):
    conn = get_db_connection()
    reviews = conn.execute("""
        SELECT * FROM salon_reviews 
        WHERE salon_id = ?
        ORDER BY created_at DESC
        LIMIT 50
    """, (salon_id,)).fetchall()

    salon = conn.execute("SELECT rating, reviews_count FROM salons WHERE id = ?", (salon_id,)).fetchone()
    conn.close()

    return jsonify({
        "rating": salon["rating"] if salon else 4.9,
        "reviews_count": salon["reviews_count"] if salon else len(reviews),
        "reviews": [dict(r) for r in reviews]
    })


# -------------------------------------------------------------
# 15. SUPER ADMIN MASTER AUTHENTICATION & SUBSCRIPTIONS
# -------------------------------------------------------------
@app.route("/admin/login", methods=["GET"])
def admin_login_page():
    # If already logged in, redirect to /admin
    if request.cookies.get("admin_auth_token") == "master_admin_session_valid":
        return redirect("/admin")
    return render_template("admin_login.html")


@app.route("/api/admin/login", methods=["POST"])
def admin_login_api():
    data = request.json or {}
    pin = str(data.get("pin") or "").strip()

    conn = get_db_connection()
    pin_row = conn.execute("SELECT value FROM admin_settings WHERE key = 'master_pin'").fetchone()
    conn.close()

    master_pin = pin_row["value"] if pin_row else "2026"
    if pin == master_pin:
        resp = jsonify({"success": True, "message": "تم تسجيل الدخول بنجاح!"})
        resp.set_cookie("admin_auth_token", "master_admin_session_valid", max_age=30*86400, httponly=True)
        return resp
    else:
        return jsonify({"error": "رمز المرور السري (Master PIN) غير صحيح"}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout_api():
    resp = jsonify({"success": True, "message": "تم تسجيل الخروج"})
    resp.delete_cookie("admin_auth_token")
    return resp


@app.route("/api/admin/salon/subscription", methods=["POST"])
def update_salon_subscription():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    salon_id = data.get("salon_id")
    action = data.get("action")  # 'suspend' or 'activate'
    extend_days = int(data.get("extend_days", 30))

    if not salon_id or action not in ("suspend", "activate"):
        return jsonify({"error": "بيانات الطلب غير صالحة"}), 400

    conn = get_db_connection()
    if action == "suspend":
        conn.execute("UPDATE salons SET subscription_status = 'suspended' WHERE id = ?", (salon_id,))
        new_status = "suspended"
        new_expiry = None
    else:
        new_expiry = (date.today() + timedelta(days=extend_days)).strftime("%Y-%m-%d")
        conn.execute("UPDATE salons SET subscription_status = 'active', subscription_expires_at = ? WHERE id = ?", (new_expiry, salon_id))
        new_status = "active"

    conn.commit()
    conn.close()

    broadcaster.broadcast("salon_subscription_updated", {
        "salon_id": salon_id,
        "subscription_status": new_status,
        "subscription_expires_at": new_expiry
    })

    return jsonify({
        "success": True,
        "subscription_status": new_status,
        "subscription_expires_at": new_expiry,
        "message": f"تم {'تجميد حساب الصالون بنجاح 🚫' if action == 'suspend' else f'تفعيل وتمديد الاشتراك لـ {extend_days} يوماً ✅'}"
    })


@app.route("/api/admin/settings", methods=["POST"])
def update_admin_settings():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح"}), 401

    data = request.json or {}
    new_pin = str(data.get("master_pin") or "").strip()
    admin_phone = str(data.get("admin_phone") or "").strip()
    admin_rip = str(data.get("admin_rip") or "").strip()

    conn = get_db_connection()
    if new_pin:
        conn.execute("INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('master_pin', ?)", (new_pin,))
    if admin_phone:
        conn.execute("INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('admin_phone', ?)", (admin_phone,))
    if admin_rip:
        conn.execute("INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('admin_rip', ?)", (admin_rip,))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "تم تحديث إعدادات المدير العام بنجاح!"})


@app.route("/api/admin/orders", methods=["GET"])
def get_admin_orders():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    conn = get_db_connection()
    orders_rows = conn.execute("""
        SELECT o.*, s.name as salon_name, s.barber_name, s.phone as barber_phone, s.wilaya, s.commune
        FROM wholesale_orders o
        LEFT JOIN salons s ON o.salon_id = s.id
        ORDER BY o.ordered_at DESC
    """).fetchall()

    orders_list = [dict(r) for r in orders_rows]

    total_orders = len(orders_list)
    pending_count = sum(1 for o in orders_list if o["status"] == "pending")
    shipped_count = sum(1 for o in orders_list if o["status"] == "shipped")
    delivered_count = sum(1 for o in orders_list if o["status"] == "delivered")
    cancelled_count = sum(1 for o in orders_list if o["status"] == "cancelled")

    active_orders = [o for o in orders_list if o["status"] != "cancelled"]
    total_wholesale_revenue = sum(o["wholesale_price"] or 0.0 for o in active_orders)
    total_platform_profit = sum(o["platform_profit"] or 0.0 for o in active_orders)

    products_rows = conn.execute("SELECT * FROM products ORDER BY type DESC, price ASC").fetchall()
    products_list = [dict(p) for p in products_rows]

    conn.close()

    return jsonify({
        "success": True,
        "orders": orders_list,
        "summary": {
            "total_orders": total_orders,
            "pending_count": pending_count,
            "shipped_count": shipped_count,
            "delivered_count": delivered_count,
            "cancelled_count": cancelled_count,
            "total_wholesale_revenue": total_wholesale_revenue,
            "total_platform_profit": total_platform_profit
        },
        "products": products_list
    })


@app.route("/api/admin/orders/status", methods=["POST"])
def update_order_status():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    order_id = data.get("order_id")
    new_status = data.get("status")

    if not order_id or new_status not in ("pending", "shipped", "delivered", "cancelled"):
        return jsonify({"error": "بيانات غير صالحة"}), 400

    conn = get_db_connection()
    conn.execute("UPDATE wholesale_orders SET status = ? WHERE id = ?", (new_status, order_id))
    conn.commit()
    conn.close()

    broadcaster.broadcast("order_status_updated", {"order_id": order_id, "status": new_status})

    status_labels = {
        "pending": "قيد المعالجة (En attente)",
        "shipped": "قيد الشحن والتوصيل (Expédiée)",
        "delivered": "تم التسليم وقبض المبلغ (Livrée & Encaissée)",
        "cancelled": "ملغاة (Annulée)"
    }
    return jsonify({
        "success": True,
        "message": f"تم تحديث حالة الطلبية إلى: {status_labels.get(new_status, new_status)}"
    })


@app.route("/api/admin/products/update", methods=["POST"])
def update_product_admin():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    product_id = data.get("product_id")
    price = data.get("price")
    profit = data.get("commission_or_profit")
    stock_count = data.get("stock_count")

    if not product_id:
        return jsonify({"error": "product_id is required"}), 400

    conn = get_db_connection()
    conn.execute("""
        UPDATE products 
        SET price = COALESCE(?, price),
            commission_or_profit = COALESCE(?, commission_or_profit),
            stock_count = COALESCE(?, stock_count)
        WHERE id = ?
    """, (price, profit, stock_count, product_id))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "تم تحديث بيانات المنتج والمخزون بنجاح!"})


@app.route("/api/admin/freelancer/verify", methods=["POST"])
def admin_toggle_freelancer_verify():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401
    data = request.json or {}
    barber_id = data.get("barber_id")
    if not barber_id:
        return jsonify({"error": "barber_id required"}), 400
    conn = get_db_connection()
    b = conn.execute("SELECT is_verified FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    if not b:
        conn.close()
        return jsonify({"error": "Freelancer not found"}), 404
    new_verified = 0 if b["is_verified"] == 1 else 1
    conn.execute("UPDATE freelance_barbers SET is_verified = ? WHERE id = ?", (new_verified, barber_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "is_verified": new_verified})


@app.route("/api/admin/freelancer/delete", methods=["POST"])
def admin_delete_freelancer():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401
    data = request.json or {}
    barber_id = data.get("barber_id")
    if not barber_id:
        return jsonify({"error": "barber_id required"}), 400
    conn = get_db_connection()
    conn.execute("DELETE FROM freelance_barbers WHERE id = ?", (barber_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "تم حذف الحلاق بنجاح"})


@app.route("/api/admin/freelancers", methods=["GET"])
def admin_get_freelancers():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401
    conn = get_db_connection()
    barbers = conn.execute("SELECT * FROM freelance_barbers ORDER BY created_at DESC").fetchall()
    result = []
    for b in barbers:
        b_dict = dict(b)
        stats = conn.execute("SELECT COUNT(*), COALESCE(SUM(price), 0.0) FROM home_service_requests WHERE barber_id = ? AND status = 'completed'", (b['id'],)).fetchone()
        b_dict["completed_count"] = stats[0]
        b_dict["gross_earnings"] = stats[1]
        b_dict["platform_commission"] = stats[1] * 0.10
        b_dict["net_earnings"] = stats[1] * 0.90
        result.append(b_dict)
    conn.close()
    return jsonify({"freelancers": result})


@app.route("/api/freelance/wallet/recharge", methods=["POST"])
def freelance_wallet_recharge():
    data = request.json or {}
    barber_id = data.get("barber_id")
    amount = float(data.get("amount", 2000.0))
    transaction_ref = data.get("transaction_ref", "").strip()
    notes = data.get("notes", "").strip()
    receipt_image = data.get("receipt_image", "").strip()

    if not barber_id or not transaction_ref:
        return jsonify({"error": "barber_id and transaction_ref required"}), 400

    auth_cookie = request.cookies.get("freelance_auth_token")
    is_admin = (request.cookies.get("admin_auth_token") == "master_admin_session_valid")
    if not is_admin and auth_cookie != f"free_{barber_id}":
        return jsonify({"error": "غير مصرح - لا يمكنك شحن رصيد حلاق آخر (IDOR Blocked)"}), 403

    conn = get_db_connection()
    recharge_id = f"rech_{uuid.uuid4().hex[:8]}"
    conn.execute("""
        INSERT INTO freelancer_wallet_recharges (id, barber_id, amount, transaction_ref, notes, receipt_image, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (recharge_id, barber_id, amount, transaction_ref, notes, receipt_image))
    conn.commit()
    conn.close()

    broadcaster.broadcast("wallet_recharge_requested", {"recharge_id": recharge_id, "barber_id": barber_id, "amount": amount})
    return jsonify({"success": True, "recharge_id": recharge_id, "message": "تم إرسال طلب الشحن بنجاح"})


@app.route("/api/admin/recharge/approve", methods=["POST"])
def admin_approve_recharge():
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return jsonify({"error": "غير مصرح - يرجى تسجيل الدخول كأدمن"}), 401

    data = request.json or {}
    recharge_id = data.get("recharge_id")
    if not recharge_id:
        return jsonify({"error": "recharge_id required"}), 400

    conn = get_db_connection()
    rech = conn.execute("SELECT * FROM freelancer_wallet_recharges WHERE id = ?", (recharge_id,)).fetchone()
    if not rech:
        conn.close()
        return jsonify({"error": "Recharge not found"}), 404

    amount = rech["amount"]
    barber_id = rech["barber_id"]

    conn.execute("UPDATE freelancer_wallet_recharges SET status = 'approved', approved_at = CURRENT_TIMESTAMP WHERE id = ?", (recharge_id,))
    conn.execute("UPDATE freelance_barbers SET wallet_balance = COALESCE(wallet_balance, 0.0) + ? WHERE id = ?", (amount, barber_id))
    conn.commit()

    updated_barber = conn.execute("SELECT wallet_balance FROM freelance_barbers WHERE id = ?", (barber_id,)).fetchone()
    new_bal = updated_barber["wallet_balance"] if updated_barber else 0.0
    conn.close()

    broadcaster.broadcast("recharge_approved", {"recharge_id": recharge_id, "barber_id": barber_id, "new_balance": new_bal})
    return jsonify({"success": True, "recharge_id": recharge_id, "new_balance": new_bal, "message": f"تم تفعيل الشحن بنجاح (+{int(amount)} د.ج)"})


@app.route("/admin")
def admin_platform():
    # 🔐 Verify Master Admin Session
    if request.cookies.get("admin_auth_token") != "master_admin_session_valid":
        return redirect("/admin/login")

    conn = get_db_connection()
    salons_raw = conn.execute("SELECT * FROM salons").fetchall()
    admin_settings_rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
    settings_map = {r["key"]: r["value"] for r in admin_settings_rows}

    salons_list = []
    for s in salons_raw:
        s_dict = dict(s)
        waiting_count = conn.execute(
            "SELECT count(*) FROM queue_tickets WHERE salon_id = ? AND status IN ('waiting', 'turn_notified', 'arrived')",
            (s['id'],)
        ).fetchone()[0]
        completed_count = conn.execute(
            "SELECT count(*) FROM queue_tickets WHERE salon_id = ? AND status = 'completed'",
            (s['id'],)
        ).fetchone()[0]
        s_dict['waiting_count'] = waiting_count
        s_dict['completed_count'] = completed_count
        salons_list.append(s_dict)

    # Wholesale orders and store metrics
    orders_raw = conn.execute("""
        SELECT o.*, s.name as salon_name, s.barber_name, s.phone as barber_phone, s.wilaya, s.commune
        FROM wholesale_orders o
        LEFT JOIN salons s ON o.salon_id = s.id
        ORDER BY o.ordered_at DESC
    """).fetchall()
    orders_list = [dict(o) for o in orders_raw]

    # Products catalog
    products_raw = conn.execute("SELECT * FROM products ORDER BY type DESC, price ASC").fetchall()
    products_list = [dict(p) for p in products_raw]

    # Freelance mobile barbers & platform commission
    freelancers_raw = conn.execute("SELECT * FROM freelance_barbers ORDER BY created_at DESC").fetchall()
    freelancers_list = []
    for f in freelancers_raw:
        f_dict = dict(f)
        stats = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(price), 0.0) 
            FROM home_service_requests 
            WHERE barber_id = ? AND status = 'completed'
        """, (f_dict['id'],)).fetchone()
        f_dict['completed_jobs_count'] = stats[0]
        f_dict['gross_earnings'] = stats[1]
        f_dict['platform_commission'] = stats[1] * 0.10
        f_dict['net_barber_take'] = stats[1] * 0.90
        freelancers_list.append(f_dict)

    # Home service requests
    home_requests_raw = conn.execute("""
        SELECT r.*, b.name as barber_name, b.phone as barber_phone, b.vehicle_type as barber_vehicle
        FROM home_service_requests r
        LEFT JOIN freelance_barbers b ON r.barber_id = b.id
        ORDER BY r.created_at DESC
    """).fetchall()
    home_requests_list = [dict(hr) for hr in home_requests_raw]

    # Freelancer wallet recharge requests
    recharges_raw = conn.execute("""
        SELECT rc.*, b.name as barber_name, b.phone as barber_phone, b.commune
        FROM freelancer_wallet_recharges rc
        LEFT JOIN freelance_barbers b ON rc.barber_id = b.id
        ORDER BY rc.created_at DESC
    """).fetchall()
    recharges_list = [dict(rc) for rc in recharges_raw]

    # Freelancer National ID & Equipment KYC Verifications
    kyc_raw = conn.execute("""
        SELECT id, name, phone, wilaya, commune, vehicle_type, id_card_number, id_card_front, id_card_back, selfie_photo, equipment_photo, kyc_status, kyc_submitted_at, is_verified
        FROM freelance_barbers
        WHERE kyc_status IN ('pending', 'rejected') OR id_card_number != ''
        ORDER BY CASE WHEN kyc_status = 'pending' THEN 1 ELSE 2 END, created_at DESC
    """).fetchall()
    kyc_list = [dict(k) for k in kyc_raw]

    conn.close()
    return render_template(
        "admin.html", 
        salons=salons_list, 
        admin_settings=settings_map, 
        orders=orders_list,
        products=products_list,
        freelancers=freelancers_list,
        home_requests=home_requests_list,
        recharges=recharges_list,
        kyc_requests=kyc_list,
        local_ip=get_local_ip()
    )


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


if __name__ == "__main__":
    init_db()
    local_ip = get_local_ip()
    print("==================================================")
    print("  🚀 سيرفر تطبيق دورك (BarberQueue) يعمل الآن بنجاح!")
    print(f"  • شاشة الحلاق:         http://localhost:5000/barber")
    print(f"  • شاشة الزبون:         http://localhost:5000/")
    print(f"  • لوحة المنصة:         http://localhost:5000/admin")
    print(f"  • تجربة الهاتف عبر Wi-Fi: http://{local_ip}:5000/")
    print("==================================================")

    import webbrowser
    import threading
    def auto_open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open("http://localhost:5000/")
        except Exception:
            pass

    threading.Thread(target=auto_open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
