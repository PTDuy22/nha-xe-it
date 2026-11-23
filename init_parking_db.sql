-- ==============================================================================
-- CƠ SỞ DỮ LIỆU: parking_db (HỆ THỐNG QUẢN LÝ BÃI GIỮ XE CHUNG CƯ THÔNG MINH)
-- Thiết kế chuẩn theo sơ đồ ERD & tích hợp AI Face Recognition (pgvector)
-- ==============================================================================

-- 1. Kích hoạt Extension pgvector để lưu trữ & so khớp vector khuôn mặt (512 chiều)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Bảng USERS: Cư dân & Tài khoản hệ thống
CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    apartment VARCHAR(50) NOT NULL,               -- Căn hộ (VD: P.1204 - Tòa A)
    phone VARCHAR(20),
    username VARCHAR(50) UNIQUE,
    password_hash VARCHAR(255),
    role VARCHAR(20) DEFAULT 'resident' CHECK (role IN ('resident', 'admin', 'security')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Bảng CARDS: Quản lý thẻ RFID
CREATE TABLE IF NOT EXISTS cards (
    card_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
    uid_rfid VARCHAR(50) UNIQUE NOT NULL,         -- Mã UID thẻ RFID (VD: 83A12BC4)
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'locked', 'lost')),
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Bảng VEHICLES: Quản lý phương tiện của cư dân
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
    card_id INT REFERENCES cards(card_id) ON DELETE SET NULL,
    plate_number VARCHAR(20) UNIQUE NOT NULL,     -- Biển số xe (VD: 59P1-123.45)
    vehicle_type VARCHAR(20) DEFAULT 'xe máy' CHECK (vehicle_type IN ('xe máy', 'ô tô', 'xe đạp điện')),
    is_inside BOOLEAN DEFAULT FALSE,              -- Cờ theo dõi xe đang đỗ trong bãi hay đã ra ngoài
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Bảng FACE_EMBEDDINGS: Vector đặc trưng khuôn mặt (Dự phòng khi biển số mờ/không khớp)
CREATE TABLE IF NOT EXISTS face_embeddings (
    face_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
    embedding vector(512) NOT NULL,               -- 512 chiều trích xuất từ FaceNet
    image_path VARCHAR(255),                      -- Đường dẫn ảnh mẫu khuôn mặt
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index tăng tốc tìm kiếm vector theo thuật toán HNSW (Cosine Distance)
CREATE INDEX IF NOT EXISTS idx_face_embeddings_hnsw 
ON face_embeddings USING hnsw (embedding vector_cosine_ops);

-- 6. Bảng GATES: Quản lý các cổng vào/ra (Edge Gateways)
CREATE TABLE IF NOT EXISTS gates (
    gate_id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,                    -- VD: Cổng Vào 01 (Làn Xe Máy)
    api_key_hash VARCHAR(255),                    -- Xác thực kết nối từ Edge Controller
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'maintenance', 'inactive'))
);

-- 7. Bảng PARKING_LOGS: Lịch sử xe vào/ra
CREATE TABLE IF NOT EXISTS parking_logs (
    log_id BIGSERIAL PRIMARY KEY,
    event_id VARCHAR(64) UNIQUE,                  -- UUID/Hash chống gửi trùng lặp từ Edge
    gate_id INT REFERENCES gates(gate_id) ON DELETE SET NULL,
    card_id INT REFERENCES cards(card_id) ON DELETE SET NULL,
    vehicle_id INT REFERENCES vehicles(vehicle_id) ON DELETE SET NULL,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('IN', 'OUT')),
    plate_read VARCHAR(20),                       -- Biển số AI OCR đọc được thực tế
    sim_bs FLOAT,                                 -- Độ tương đồng biển số (%)
    sim_km FLOAT,                                 -- Độ tương đồng khuôn mặt (%)
    face_used BOOLEAN DEFAULT FALSE,              -- True nếu phải kích hoạt AI nhận diện mặt dự phòng
    result VARCHAR(10) NOT NULL CHECK (result IN ('ALLOWED', 'DENIED')),
    reason VARCHAR(255),                          -- Lý do từ chối (VD: Thẻ bị khóa, Biển số không khớp,...)
    plate_image_path VARCHAR(255),                -- Ảnh chụp biển số lúc vào/ra
    face_image_path VARCHAR(255),                 -- Ảnh chụp khuôn mặt (nếu có kích hoạt)
    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index tra cứu log nhanh theo thời gian và biển số
CREATE INDEX IF NOT EXISTS idx_parking_logs_time ON parking_logs(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_parking_logs_plate ON parking_logs(plate_read);

-- ==============================================================================
-- KHỞI TẠO TÀI KHOẢN ADMIN VÀ CỔNG HỆ THỐNG
-- ==============================================================================

INSERT INTO users (user_id, full_name, apartment, phone, username, password_hash, role) VALUES
(1, 'Ban Quản Lý Chung Cư', 'BQL-01', '0901234567', 'admin', 'pbkdf2:sha256:admin', 'admin')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO gates (gate_id, name, api_key_hash, status) VALUES
(1, 'Cổng Vào 01 (Làn Xe Máy)', 'gate_key_lane1_in', 'active'),
(2, 'Cổng Ra 01 (Làn Xe Máy)', 'gate_key_lane1_out', 'active'),
(3, 'Cổng Vào 02 (Làn Ô Tô)', 'gate_key_lane2_in', 'active'),
(4, 'Cổng Ra 02 (Làn Ô Tô)', 'gate_key_lane2_out', 'active')
ON CONFLICT (gate_id) DO NOTHING;

SELECT setval('users_user_id_seq', (SELECT COALESCE(MAX(user_id), 1) FROM users));
SELECT setval('gates_gate_id_seq', (SELECT COALESCE(MAX(gate_id), 1) FROM gates));

-- 8. Bảng SCAN_BUFFER: lưu mã thẻ quẹt gần nhất (thay biến toàn cục, vì Vercel là serverless)
CREATE TABLE IF NOT EXISTS scan_buffer (
    id INT PRIMARY KEY,
    uid VARCHAR(50) NOT NULL,
    ts DOUBLE PRECISION NOT NULL
);
