import base64
import csv
import hashlib
import io
import os
import sqlite3
from datetime import datetime, timezone
from io import BytesIO

import qrcode
from flask import Flask, Response, abort, render_template, request, redirect, session, url_for
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.secret_key = "internship-perks-demo-secret"
DB_PATH = os.path.join(os.path.dirname(__file__), "internship_app.db")
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def current_utc_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def generate_unique_id(applicant_id):
    return f"INT-{datetime.now(timezone.utc).year}-{applicant_id:05d}"


def generate_qr_data_url(value):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(value)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def generate_pdf_response(title, subtitle, lines, qr_value=None, applicant=None):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setTitle(title)
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(70, height - 70, title)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(70, height - 100, subtitle)

    if applicant:
        pdf.drawString(70, height - 135, f"Name: {applicant['full_name']}")
        pdf.drawString(70, height - 155, f"Email: {applicant['email']}")
        pdf.drawString(70, height - 175, f"Internship Type: {applicant['internship_type'] or 'General Internship'}")
        pdf.drawString(70, height - 195, f"Unique ID: {applicant['unique_id'] or '-'}")

    y = height - 240
    for line in lines:
        if y < 70:
            pdf.showPage()
            y = height - 70
        pdf.drawString(70, y, line)
        y -= 18

    if qr_value:
        qr_img = qrcode.make(qr_value)
        qrcode_buffer = BytesIO()
        qr_img.save(qrcode_buffer, format="PNG")
        qrcode_buffer.seek(0)
        pdf.drawInlineImage(Image.open(qrcode_buffer), 430, 80, width=110, height=110)

    pdf.save()
    pdf_bytes = buffer.getvalue()
    response = Response(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="{title.lower().replace(" ", "_")}.pdf"'
    return response


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applicants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            university TEXT,
            course TEXT,
            internship_type TEXT,
            motivation TEXT,
            unique_id TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
        """
    )

    applicant_columns = {row[1] for row in conn.execute("PRAGMA table_info(applicants)").fetchall()}
    for column_name, column_sql in {
        "status": "ALTER TABLE applicants ADD COLUMN status TEXT DEFAULT 'pending'",
        "unique_id": "ALTER TABLE applicants ADD COLUMN unique_id TEXT UNIQUE",
        "submitted_at": "ALTER TABLE applicants ADD COLUMN submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    }.items():
        if column_name not in applicant_columns:
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass

    admin_exists = conn.execute(
        "SELECT 1 FROM admins WHERE username = ?",
        (DEFAULT_ADMIN_USERNAME,),
    ).fetchone()
    if not admin_exists:
        conn.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)),
        )

    conn.commit()
    conn.close()


@app.before_request
def ensure_database_ready():
    init_db()


init_db()


def admin_required(view_func):
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    wrapper.__name__ = view_func.__name__
    return wrapper


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/apply", methods=["POST"])
def apply():
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    university = (request.form.get("university") or "").strip()
    course = (request.form.get("course") or "").strip()
    internship_type = (request.form.get("internship_type") or "").strip()
    motivation = (request.form.get("motivation") or "").strip()

    if not full_name or not email:
        return "Name and email are required.", 400

    conn = get_db_connection()
    existing = conn.execute(
        "SELECT 1 FROM applicants WHERE email = ? LIMIT 1",
        (email,),
    ).fetchone()
    if existing:
        conn.close()
        return "An application with this email already exists.", 409

    try:
        cursor = conn.execute(
            """
            INSERT INTO applicants (full_name, email, phone, university, course, internship_type, motivation, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                full_name,
                email,
                phone,
                university,
                course,
                internship_type,
                motivation,
                current_utc_timestamp(),
            ),
        )
        applicant_id = cursor.lastrowid
        unique_id = generate_unique_id(applicant_id)
        conn.execute(
            "UPDATE applicants SET unique_id = ? WHERE id = ?",
            (unique_id, applicant_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return "This application could not be saved. Please try again.", 400
    finally:
        conn.close()

    return redirect(url_for("thanks", name=full_name))


@app.route("/thanks")
def thanks():
    name = request.args.get("name", "Applicant")
    return render_template("thanks.html", name=name)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        conn = get_db_connection()
        admin = conn.execute(
            "SELECT * FROM admins WHERE username = ? AND password_hash = ?",
            (username, hash_password(password)),
        ).fetchone()
        conn.close()

        if admin:
            session["admin_logged_in"] = True
            session["admin_username"] = username
            return redirect(url_for("admin_dashboard"))

        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    status_filter = request.args.get("status", "all")
    search = request.args.get("search", "")

    query = "SELECT * FROM applicants WHERE 1=1"
    params = []

    if status_filter != "all":
        query += " AND status = ?"
        params.append(status_filter)

    if search:
        query += " AND (full_name LIKE ? OR email LIKE ? OR university LIKE ? OR internship_type LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term, term])

    query += " ORDER BY submitted_at DESC"

    conn = get_db_connection()
    applicants = conn.execute(query, params).fetchall()
    summary = conn.execute(
        "SELECT status, COUNT(*) AS total FROM applicants GROUP BY status"
    ).fetchall()
    conn.close()

    status_counts = {"pending": 0, "approved": 0, "accepted": 0, "rejected": 0}
    for row in summary:
        status_counts[row["status"]] = row["total"]

    return render_template(
        "admin.html",
        applicants=applicants,
        admin_username=session.get("admin_username"),
        status_filter=status_filter,
        search=search,
        status_counts=status_counts,
    )


@app.route("/admin/export/csv")
@admin_required
def export_applicants_csv():
    conn = get_db_connection()
    applicants = conn.execute(
        "SELECT * FROM applicants ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "full_name",
        "email",
        "phone",
        "university",
        "course",
        "internship_type",
        "motivation",
        "unique_id",
        "status",
        "submitted_at",
    ])

    for applicant in applicants:
        writer.writerow([
            applicant["id"],
            applicant["full_name"],
            applicant["email"],
            applicant["phone"],
            applicant["university"],
            applicant["course"],
            applicant["internship_type"],
            applicant["motivation"],
            applicant["unique_id"],
            applicant["status"],
            applicant["submitted_at"],
        ])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=internship_applications.csv"
    return response


@app.route("/admin/applicant/<int:applicant_id>")
@admin_required
def applicant_detail(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    return render_template(
        "applicant_detail.html",
        applicant=dict(applicant),
        admin_username=session.get("admin_username"),
    )


@app.route("/admin/applicant/<int:applicant_id>/status", methods=["POST"])
@admin_required
def applicant_status(applicant_id):
    status_value = (request.form.get("status") or "pending").strip()
    allowed = {"pending", "approved", "accepted", "rejected"}

    if status_value not in allowed:
        abort(400)

    conn = get_db_connection()
    conn.execute("UPDATE applicants SET status = ? WHERE id = ?", (status_value, applicant_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/applicant/<int:applicant_id>/certificate")
@admin_required
def applicant_certificate(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    qr_url = generate_qr_data_url(f"CERT-{applicant['unique_id']}")
    return render_template(
        "certificate.html",
        applicant=dict(applicant),
        qr_data_url=qr_url,
        generated_date=datetime.now(timezone.utc).strftime("%d %B %Y"),
    )


@app.route("/admin/applicant/<int:applicant_id>/certificate.pdf")
@admin_required
def applicant_certificate_pdf(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    lines = [
        "This is to certify that the above named candidate successfully completed the internship program.",
        "Program: Internship Perks",
        "Verification ID: CERT-" + (applicant["unique_id"] or "-"),
        "This certification is issued under the internship program standards.",
    ]
    return generate_pdf_response(
        "Completion Certificate",
        "Internship Perks Program",
        lines,
        qr_value=f"CERT-{applicant['unique_id']}",
        applicant=dict(applicant),
    )


@app.route("/admin/applicant/<int:applicant_id>/offer-letter")
@admin_required
def applicant_offer_letter(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    return render_template(
        "offer_letter.html",
        applicant=dict(applicant),
        issued_date=datetime.now(timezone.utc).strftime("%d %B %Y"),
    )


@app.route("/admin/applicant/<int:applicant_id>/offer-letter.pdf")
@admin_required
def applicant_offer_letter_pdf(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    lines = [
        "We are pleased to offer this candidate an internship placement in the Internship Perks Program.",
        f"Candidate: {applicant['full_name']}",
        f"Email: {applicant['email']}",
        f"Internship Type: {applicant['internship_type'] or 'General Internship'}",
        "This offer is valid under the standard terms of the internship program.",
    ]
    return generate_pdf_response(
        "Internship Offer Letter",
        "Internship Perks Program",
        lines,
        applicant=dict(applicant),
    )


@app.route("/admin/applicant/<int:applicant_id>/recommendation")
@admin_required
def applicant_recommendation(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    return render_template(
        "recommendation.html",
        applicant=dict(applicant),
        issued_date=datetime.now(timezone.utc).strftime("%d %B %Y"),
    )


@app.route("/admin/applicant/<int:applicant_id>/recommendation.pdf")
@admin_required
def applicant_recommendation_pdf(applicant_id):
    conn = get_db_connection()
    applicant = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
    conn.close()

    if applicant is None:
        abort(404)

    lines = [
        "This letter confirms the candidate's professionalism, commitment, and strong engagement during the internship program.",
        f"Candidate: {applicant['full_name']}",
        f"Internship Type: {applicant['internship_type'] or 'General Internship'}",
        "We recommend the candidate for future opportunities based on their performance and potential.",
    ]
    return generate_pdf_response(
        "Letter of Recommendation",
        "Internship Perks Program",
        lines,
        applicant=dict(applicant),
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
