# app.py
from flask import Flask, jsonify, abort, send_file
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

_engine = None

def get_engine():
    global _engine
    if _engine is not None:
        return _engine
    db_url = os.getenv("DB_URL")
    if not db_url:
        raise RuntimeError("Missing DB_URL (or DATABASE_URL) environment variable.")
    # Normalize old 'postgres://' scheme to 'postgresql://'
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    _engine = create_engine(
        db_url,
        pool_pre_ping=True,
    )
    return _engine

def create_app():
    app = Flask(__name__)

    @app.get("/", endpoint="health")
    def health():
        return "<p>Server working!</p>"

    @app.get("/img", endpoint="show_img")
    def show_img():
        return send_file("amygdala.gif", mimetype="image/gif")

    @app.get("/terms/<term>/studies", endpoint="terms_studies")
    def get_studies_by_term(term):
        return term

    @app.get("/locations/<coords>/studies", endpoint="locations_studies")
    def get_studies_by_coordinates(coords):
        x, y, z = map(int, coords.split("_"))
        return jsonify([x, y, z])

    @app.get("/test_db", endpoint="test_db")
    def test_db():
        eng = get_engine()
        payload = {"ok": False, "dialect": eng.dialect.name}

        try:
            with eng.begin() as conn:
                # Ensure we are in the correct schema
                conn.execute(text("SET search_path TO ns, public;"))
                payload["version"] = conn.exec_driver_sql("SELECT version()").scalar()

                # Counts
                payload["coordinates_count"] = conn.execute(text("SELECT COUNT(*) FROM ns.coordinates")).scalar()
                payload["metadata_count"] = conn.execute(text("SELECT COUNT(*) FROM ns.metadata")).scalar()
                payload["annotations_terms_count"] = conn.execute(text("SELECT COUNT(*) FROM ns.annotations_terms")).scalar()

                # Samples
                try:
                    rows = conn.execute(text(
                        "SELECT study_id, ST_X(geom) AS x, ST_Y(geom) AS y, ST_Z(geom) AS z FROM ns.coordinates LIMIT 3"
                    )).mappings().all()
                    payload["coordinates_sample"] = [dict(r) for r in rows]
                except Exception:
                    payload["coordinates_sample"] = []

                try:
                    # Select a few columns if they exist; otherwise select a generic subset
                    rows = conn.execute(text("SELECT * FROM ns.metadata LIMIT 3")).mappings().all()
                    payload["metadata_sample"] = [dict(r) for r in rows]
                except Exception:
                    payload["metadata_sample"] = []

                try:
                    rows = conn.execute(text(
                        "SELECT study_id, contrast_id, term, weight FROM ns.annotations_terms LIMIT 3"
                    )).mappings().all()
                    payload["annotations_terms_sample"] = [dict(r) for r in rows]
                except Exception:
                    payload["annotations_terms_sample"] = []

            payload["ok"] = True
            return jsonify(payload), 200

        except Exception as e:
            payload["error"] = str(e)
            return jsonify(payload), 500

    @app.get("/dissociate/terms/<term_a>/<term_b>", endpoint="dissociate_terms")
    def dissociate_terms(term_a, term_b):
        """
        Returns studies that mention term_a (in title OR abstract) 
        but NOT term_b (in title OR abstract)
        """
        eng = get_engine()
        
        try:
            with eng.begin() as conn:
                conn.execute(text("SET search_path TO ns, public;"))
                
                # 🔥 關鍵改進：同時查詢 title 和 abstract
                # 使用 ILIKE 進行不區分大小寫的模糊匹配
                query_template = text("""
                    SELECT DISTINCT m.study_id, m.title
                    FROM ns.metadata m
                    LEFT JOIN ns.annotations_terms at ON m.study_id = at.study_id
                    WHERE 
                        -- 在 title 中找 (原始輸入格式)
                        m.title ILIKE :term_pattern
                        OR 
                        -- 在 abstract annotations 中找 (TF-IDF 格式)
                        at.term = :term_tfidf
                """)
                
                # 準備搜尋參數
                term_a_pattern = f"%{term_a.replace('_', ' ')}%"  # "posterior_cingulate" -> "%posterior cingulate%"
                term_a_tfidf = f"terms_abstract_tfidf__{term_a.lower().replace('-', '_')}"
                
                term_b_pattern = f"%{term_b.replace('_', ' ')}%"
                term_b_tfidf = f"terms_abstract_tfidf__{term_b.lower().replace('-', '_')}"
                
                # 查詢包含 term_a 的研究
                result_a = conn.execute(
                    query_template, 
                    {"term_pattern": term_a_pattern, "term_tfidf": term_a_tfidf}
                ).fetchall()
                studies_a = {row[0]: row[1] for row in result_a}  # {study_id: title}
                
                # 查詢包含 term_b 的研究
                result_b = conn.execute(
                    query_template,
                    {"term_pattern": term_b_pattern, "term_tfidf": term_b_tfidf}
                ).fetchall()
                studies_b = set(row[0] for row in result_b)
                
                # 集合運算：A - B（有 A 但沒有 B）
                difference_ids = set(studies_a.keys()) - studies_b
                
                # 🎁 加碼：返回詳細資訊（含 title 和 weight）
                detailed_results = []
                for study_id in list(difference_ids)[:100]:  # 限制 100 筆
                    # 取得這個 study 的 term_a weight（如果有的話）
                    weight_query = text("""
                        SELECT weight 
                        FROM ns.annotations_terms 
                        WHERE study_id = :sid AND term = :term
                        LIMIT 1
                    """)
                    weight_result = conn.execute(
                        weight_query, 
                        {"sid": study_id, "term": term_a_tfidf}
                    ).fetchone()
                    
                    detailed_results.append({
                        "study_id": study_id,
                        "title": studies_a[study_id],
                        "weight": weight_result[0] if weight_result else None
                    })
                
                return jsonify({
                    "term_a": term_a,
                    "term_b": term_b,
                    "total_count": len(difference_ids),
                    "results": detailed_results
                })
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/dissociate/locations/<coords_a>/<coords_b>", endpoint="dissociate_locations")
    def dissociate_locations(coords_a, coords_b):
        """
        Returns studies near coords_a but NOT near coords_b
        Uses 10mm radius for proximity
        """
        eng = get_engine()
        
        try:
            # Parse coordinates
            x1, y1, z1 = map(float, coords_a.split("_"))
            x2, y2, z2 = map(float, coords_b.split("_"))
            
            with eng.begin() as conn:
                conn.execute(text("SET search_path TO ns, public;"))
                
                # Find studies near location A
                query_a = text("""
                    SELECT DISTINCT study_id 
                    FROM ns.coordinates 
                    WHERE ST_DWithin(
                        geom, 
                        ST_SetSRID(ST_MakePoint(:x, :y, :z), 4326),
                        10
                    )
                """)
                
                result_a = conn.execute(query_a, {"x": x1, "y": y1, "z": z1}).fetchall()
                studies_a = set(row[0] for row in result_a)
                
                # Find studies near location B
                result_b = conn.execute(query_a, {"x": x2, "y": y2, "z": z2}).fetchall()
                studies_b = set(row[0] for row in result_b)
                
                # Calculate difference (A without B)
                difference = studies_a - studies_b
                
                return jsonify({
                    "location_a": [x1, y1, z1],
                    "location_b": [x2, y2, z2],
                    "studies_near_a_not_b": list(difference)[:100],
                    "count": len(difference),
                    "radius_mm": 10
                })
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return app
# WSGI entry point (no __main__)
app = create_app()