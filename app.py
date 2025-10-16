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

    # ADD THE DISSOCIATION ENDPOINTS HERE (with proper indentation)
    @app.get("/dissociate/terms/<term_a>/<term_b>", endpoint="dissociate_terms")
    def dissociate_terms(term_a, term_b):
        """
        Returns studies that mention term_a but NOT term_b
        """
        eng = get_engine()
        
        try:
            with eng.begin() as conn:
                conn.execute(text("SET search_path TO ns, public;"))
                
                # Convert terms to match database format
                term_a_clean = term_a.lower().replace("-", "_")
                term_b_clean = term_b.lower().replace("-", "_")
                
                # Query for A without B
                query = text("""
                    SELECT DISTINCT a1.study_id
                    FROM ns.annotations_terms a1
                    WHERE a1.term = :term_a
                    AND a1.study_id NOT IN (
                        SELECT study_id 
                        FROM ns.annotations_terms 
                        WHERE term = :term_b
                    )
                    LIMIT 100
                """)
                
                result = conn.execute(query, {
                    "term_a": term_a_clean,
                    "term_b": term_b_clean
                }).fetchall()
                
                study_ids = [row[0] for row in result]
                
                return jsonify({
                    "term_a": term_a,
                    "term_b": term_b,
                    "studies_with_a_not_b": study_ids,
                    "count": len(study_ids)
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
                
                # Find studies near A but not near B
                query = text("""
                    SELECT DISTINCT c1.study_id
                    FROM ns.coordinates c1
                    WHERE ST_DWithin(
                        c1.geom, 
                        ST_SetSRID(ST_MakePoint(:x1, :y1, :z1), 4326),
                        10
                    )
                    AND c1.study_id NOT IN (
                        SELECT study_id 
                        FROM ns.coordinates
                        WHERE ST_DWithin(
                            geom,
                            ST_SetSRID(ST_MakePoint(:x2, :y2, :z2), 4326),
                            10
                        )
                    )
                    LIMIT 100
                """)
                
                result = conn.execute(query, {
                    "x1": x1, "y1": y1, "z1": z1,
                    "x2": x2, "y2": y2, "z2": z2
                }).fetchall()
                
                study_ids = [row[0] for row in result]
                
                return jsonify({
                    "location_a": [x1, y1, z1],
                    "location_b": [x2, y2, z2],
                    "studies_near_a_not_b": study_ids,
                    "count": len(study_ids),
                    "radius_mm": 10
                })
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app

# WSGI entry point (no __main__)
app = create_app()