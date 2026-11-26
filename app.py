import os
import time
import math
import difflib
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")      # Neon connection string (pooled)
ADMIN_KEY = os.getenv("ADMIN_KEY", "")        # khóa cho trang web quản trị
DEVICE_KEY = os.getenv("DEVICE_API_KEY", "")  # khóa cho Raspberry Pi / đầu đọc thẻ

app = FastAPI(title="Smart Parking Management System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def require_admin(x_admin_key: str = Header(default="")):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Sai hoặc thiếu ADMIN KEY")

def require_device(x_api_key: str = Header(default="")):
    if not DEVICE_KEY or x_api_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="Sai hoặc thiếu DEVICE API KEY")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    # Giờ Việt Nam cho CURRENT_DATE / CURRENT_TIMESTAMP (SET LOCAL an toàn với pooler)
    cur.execute("SET LOCAL TIME ZONE 'Asia/Ho_Chi_Minh';")
    cur.close()
    return conn

# Trang web: Vercel thường tự phục vụ public/index.html; route này là dự phòng
from fastapi.responses import FileResponse, PlainTextResponse
_BASE = os.path.dirname(os.path.abspath(__file__))

@app.get("/", include_in_schema=False)
def serve_home():
    for p in (os.path.join(_BASE, "public", "index.html"), os.path.join(_BASE, "index.html")):
        if os.path.exists(p):
            return FileResponse(p)
    return PlainTextResponse("Không tìm thấy public/index.html trong bản deploy", status_code=404)

@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "has_db_url": bool(DATABASE_URL), "has_admin_key": bool(ADMIN_KEY), "has_device_key": bool(DEVICE_KEY)}

# Models
class ResidentRegisterRequest(BaseModel):
    full_name: str
    apartment: str
    phone: Optional[str] = None
    uid_rfid: str
    plate_number: str
    vehicle_type: str = "xe máy"
    face_embeddings: Optional[List[List[float]]] = None  # tối đa 5 vector, mỗi vector 512 số

class GateEventRequest(BaseModel):
    event_id: str
    gate_id: int
    uid_rfid: str
    direction: str  # 'IN' hoặc 'OUT'
    plate_read: Optional[str] = None
    face_embedding: Optional[List[float]] = None

@app.get("/api/dashboard/stats", dependencies=[Depends(require_admin)])
def get_dashboard_stats():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) AS total_residents FROM users WHERE role = 'resident';")
        total_residents = cur.fetchone()["total_residents"]

        cur.execute("SELECT COUNT(*) AS active_cards FROM cards WHERE status = 'active';")
        active_cards = cur.fetchone()["active_cards"]

        cur.execute("SELECT COUNT(*) AS vehicles_inside FROM vehicles WHERE is_inside = TRUE;")
        vehicles_inside = cur.fetchone()["vehicles_inside"]

        cur.execute("""
            SELECT COUNT(*) AS today_events FROM parking_logs 
            WHERE DATE(event_time) = CURRENT_DATE;
        """)
        today_events = cur.fetchone()["today_events"]

        cur.execute("""
            SELECT * FROM parking_logs 
            ORDER BY event_time DESC LIMIT 10;
        """)
        recent_logs = cur.fetchall()

        return {
            "total_residents": total_residents,
            "active_cards": active_cards,
            "vehicles_inside": vehicles_inside,
            "today_events": today_events,
            "recent_logs": recent_logs
        }
    finally:
        cur.close()
        conn.close()

# Residents list (chỉ lấy danh sách cư dân đăng ký gửi xe)
@app.get("/api/residents", dependencies=[Depends(require_admin)])
def get_residents():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.user_id, u.full_name, u.apartment, u.phone, u.username, u.role, u.created_at,
                   c.card_id, c.uid_rfid, c.status AS card_status,
                   v.vehicle_id, v.plate_number, v.vehicle_type, v.is_inside
            FROM users u
            LEFT JOIN cards c ON u.user_id = c.user_id
            LEFT JOIN vehicles v ON u.user_id = v.user_id
            WHERE u.role = 'resident'
            ORDER BY u.user_id DESC;
        """)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

# API nhận mã thẻ từ đầu đọc (lưu DB vì serverless không giữ biến toàn cục)
@app.post("/api/scan-card", dependencies=[Depends(require_device)])
def receive_scanned_card(payload: dict):
    uid = (payload.get("uid") or "").strip().upper()
    if not uid:
        return {"status": "error", "message": "UID trống"}
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO scan_buffer (id, uid, ts) VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE SET uid = EXCLUDED.uid, ts = EXCLUDED.ts;
        """, (uid, time.time()))
        conn.commit()
        return {"status": "success", "uid": uid}
    finally:
        cur.close()
        conn.close()

# API cho Website lấy mã thẻ vừa quẹt
@app.get("/api/latest-scanned-card", dependencies=[Depends(require_admin)])
def get_latest_scanned_card():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT uid, ts FROM scan_buffer WHERE id = 1;")
        row = cur.fetchone()
        return {"uid": row["uid"], "timestamp": row["ts"]} if row else {"uid": "", "timestamp": 0}
    finally:
        cur.close()
        conn.close()

# ĐĂNG KÝ GIỮ XE MỚI (Cư dân + Cấp thẻ RFID + Đăng ký biển số xe 1 bước)
@app.post("/api/register-parking", dependencies=[Depends(require_admin)])
def register_parking(data: ResidentRegisterRequest):
    # Vector khuôn mặt do tool host trên máy quản lý tính sẵn (không gửi ảnh lên server)
    faces = data.face_embeddings or []
    if len(faces) > 5:
        raise HTTPException(status_code=400, detail="Chỉ được gửi tối đa 5 vector khuôn mặt!")
    for v in faces:
        if len(v) != 512 or not all(math.isfinite(x) for x in v):
            raise HTTPException(status_code=400, detail="Mỗi vector khuôn mặt phải có đúng 512 số hợp lệ!")

    conn = get_db()
    cur = conn.cursor()
    try:
        # 1. Kiểm tra xem thẻ RFID đã tồn tại chưa
        cur.execute("SELECT card_id FROM cards WHERE uid_rfid = %s;", (data.uid_rfid.strip(),))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Mã thẻ RFID này đã được sử dụng!")

        # 2. Kiểm tra xem biển số xe đã tồn tại chưa
        cur.execute("SELECT vehicle_id FROM vehicles WHERE plate_number = %s;", (data.plate_number.strip(),))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Biển số xe này đã được đăng ký trước đó!")

        # 3. Tạo thông tin Cư dân
        cur.execute("""
            INSERT INTO users (full_name, apartment, phone, role)
            VALUES (%s, %s, %s, 'resident') RETURNING user_id;
        """, (data.full_name.strip(), data.apartment.strip(), data.phone.strip() if data.phone else None))
        user_id = cur.fetchone()["user_id"]

        # 4. Cấp Thẻ RFID
        cur.execute("""
            INSERT INTO cards (user_id, uid_rfid, status)
            VALUES (%s, %s, 'active') RETURNING card_id;
        """, (user_id, data.uid_rfid.strip()))
        card_id = cur.fetchone()["card_id"]

        # 5. Đăng ký Phương tiện gắn với thẻ
        cur.execute("""
            INSERT INTO vehicles (user_id, card_id, plate_number, vehicle_type, is_inside)
            VALUES (%s, %s, %s, %s, FALSE) RETURNING vehicle_id;
        """, (user_id, card_id, data.plate_number.strip(), data.vehicle_type))
        vehicle_id = cur.fetchone()["vehicle_id"]

        # 6. Lưu vector khuôn mặt (nếu có) vào face_embeddings
        for v in faces:
            vec_text = "[" + ",".join(repr(float(x)) for x in v) + "]"
            cur.execute("INSERT INTO face_embeddings (user_id, embedding) VALUES (%s, %s::vector);",
                        (user_id, vec_text))

        conn.commit()
        return {
            "status": "success",
            "message": "Đăng ký giữ xe thành công!",
            "face_vectors_saved": len(faces),
            "user_id": user_id,
            "card_id": card_id,
            "vehicle_id": vehicle_id
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# HỦY ĐĂNG KÝ GIỮ XE (Xóa cư dân, thu hồi thẻ RFID & phương tiện)
@app.delete("/api/residents/{user_id}", dependencies=[Depends(require_admin)])
def delete_resident(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, full_name FROM users WHERE user_id = %s;", (user_id,))
        resident = cur.fetchone()
        if not resident:
            raise HTTPException(status_code=404, detail="Không tìm thấy cư dân này!")

        # Thu hồi và xóa (do đã cấu hình ON DELETE CASCADE nên thẻ, xe, vector sẽ tự động xóa sạch)
        cur.execute("DELETE FROM users WHERE user_id = %s;", (user_id,))
        conn.commit()
        return {"status": "success", "message": f"Đã hủy đăng ký giữ xe và thu hồi thẻ của cư dân {resident['full_name']} thành công!"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# Gate Events (Core Logic cho Edge Controller / Barrier)
@app.post("/api/gate-events", dependencies=[Depends(require_device)])
def process_gate_event(event: GateEventRequest):
    conn = get_db()
    cur = conn.cursor()
    try:
        # 1. Chống gửi trùng lặp event_id
        cur.execute("SELECT log_id, result FROM parking_logs WHERE event_id = %s;", (event.event_id,))
        existing = cur.fetchone()
        if existing:
            return {"status": "ignored", "message": "Event already processed", "result": existing["result"]}

        # 2. Tra cứu Thẻ RFID
        cur.execute("""
            SELECT c.card_id, c.status, c.user_id, u.full_name, u.apartment,
                   v.vehicle_id, v.plate_number, v.is_inside
            FROM cards c
            JOIN users u ON c.user_id = u.user_id
            LEFT JOIN vehicles v ON c.card_id = v.card_id
            WHERE c.uid_rfid = %s;
        """, (event.uid_rfid,))
        card_record = cur.fetchone()

        if not card_record:
            cur.execute("""
                INSERT INTO parking_logs (event_id, gate_id, direction, plate_read, result, reason)
                VALUES (%s, %s, %s, %s, 'DENIED', 'Thẻ RFID chưa được đăng ký trong hệ thống');
            """, (event.event_id, event.gate_id, event.direction, event.plate_read))
            conn.commit()
            return {"result": "DENIED", "open_barrier": False, "reason": "Thẻ không hợp lệ"}

        if card_record["status"] != "active":
            cur.execute("""
                INSERT INTO parking_logs (event_id, gate_id, card_id, vehicle_id, direction, plate_read, result, reason)
                VALUES (%s, %s, %s, %s, %s, %s, 'DENIED', 'Thẻ RFID đang bị khóa');
            """, (event.event_id, event.gate_id, card_record["card_id"], card_record["vehicle_id"], event.direction, event.plate_read))
            conn.commit()
            return {"result": "DENIED", "open_barrier": False, "reason": "Thẻ đang bị khóa"}

        # 3. So khớp biển số xe (Decision Engine theo sơ đồ)
        reg_plate = (card_record["plate_number"] or "").replace(".", "").replace("-", "").upper()
        read_plate = (event.plate_read or "").replace(".", "").replace("-", "").upper()

        sim_bs = 0.0
        plate_match = False
        if reg_plate and read_plate:
            sim_bs = round(difflib.SequenceMatcher(None, reg_plate, read_plate).ratio() * 100, 2)
            if sim_bs >= 85.0:
                plate_match = True

        result = "DENIED"
        reason = ""
        open_barrier = False
        face_used = False
        sim_km = 0.0

        if plate_match:
            result = "ALLOWED"
            open_barrier = True
            reason = "Biển số khớp hoàn toàn"
        else:
            # 4. Fallback nhận diện khuôn mặt nếu gửi kèm vector
            if event.face_embedding and len(event.face_embedding) == 512:
                face_used = True
                cur.execute("""
                    SELECT face_id, 1 - (embedding <=> %s::vector) AS sim
                    FROM face_embeddings
                    WHERE user_id = %s
                    ORDER BY sim DESC LIMIT 1;
                """, (event.face_embedding, card_record["user_id"]))
                face_match = cur.fetchone()
                if face_match and face_match["sim"] is not None:
                    sim_km = round(face_match["sim"] * 100, 2)
                    if sim_km >= 80.0:
                        result = "ALLOWED"
                        open_barrier = True
                        reason = f"Fallback FaceID thành công ({sim_km}%)"
                    else:
                        reason = f"Biển số lệch ({sim_bs}%) & Khuôn mặt không khớp ({sim_km}%)"
                else:
                    reason = f"Biển số lệch ({sim_bs}%) & Cư dân chưa đăng ký FaceID"
            else:
                reason = f"Biển số không khớp ({sim_bs}%) và không có dữ liệu FaceID"

        # Cập nhật trạng thái đỗ xe nếu ALLOWED
        if result == "ALLOWED" and card_record["vehicle_id"]:
            new_inside_status = True if event.direction == "IN" else False
            cur.execute("UPDATE vehicles SET is_inside = %s WHERE vehicle_id = %s;", (new_inside_status, card_record["vehicle_id"]))

        # Ghi Parking Logs
        cur.execute("""
            INSERT INTO parking_logs (event_id, gate_id, card_id, vehicle_id, direction, plate_read, sim_bs, sim_km, face_used, result, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (
            event.event_id, event.gate_id, card_record["card_id"], card_record["vehicle_id"],
            event.direction, event.plate_read, sim_bs, sim_km, face_used, result, reason
        ))
        conn.commit()

        return {
            "result": result,
            "open_barrier": open_barrier,
            "reason": reason,
            "user": card_record["full_name"],
            "apartment": card_record["apartment"],
            "plate_registered": card_record["plate_number"],
            "sim_bs": sim_bs,
            "sim_km": sim_km,
            "face_used": face_used
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# Logs Tra cứu
@app.get("/api/logs", dependencies=[Depends(require_admin)])
def get_logs(limit: int = 50):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT l.*, u.full_name, u.apartment, c.uid_rfid, v.plate_number AS registered_plate, g.name AS gate_name
            FROM parking_logs l
            LEFT JOIN cards c ON l.card_id = c.card_id
            LEFT JOIN users u ON c.user_id = u.user_id
            LEFT JOIN vehicles v ON l.vehicle_id = v.vehicle_id
            LEFT JOIN gates g ON l.gate_id = g.gate_id
            ORDER BY l.event_time DESC LIMIT %s;
        """, (limit,))
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("API_PORT", 8080)), reload=True)
