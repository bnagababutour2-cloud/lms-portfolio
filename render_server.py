
from flask import Flask, render_template, request, jsonify, redirect, url_for

app = Flask(__name__)

# =========================
# ROUTES
# =========================

@app.route("/")
def login():
    return render_template("login.html")


@app.route("/client")
def client_portfolio():
    return render_template("client_portfolio.html")


@app.route("/admin")
def admin_portfolio():
    return render_template("admin_portfolio.html")


@app.route("/trade-report")
def trade_report():
    return render_template("trade_report.html")


# =========================
# API ROUTES
# =========================

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json

    client_id = data.get("client_id")
    password = data.get("password")

    # TODO:
    # Validate against PostgreSQL clients table

    return jsonify({
        "success": True,
        "client_id": client_id
    })


@app.route("/api/portfolio/<client_id>")
def get_portfolio(client_id):

    # TODO:
    # Read from portfolio table

    return jsonify([])


@app.route("/api/change-request", methods=["POST"])
def change_request():

    # TODO:
    # Insert into change_requests table

    return jsonify({
        "success": True
    })


@app.route("/api/admin/requests")
def admin_requests():

    # TODO:
    # Return pending requests

    return jsonify([])


@app.route("/api/admin/approve/<int:req_id>", methods=["POST"])
def approve_request(req_id):

    # TODO:
    # Update status APPROVED

    return jsonify({
        "success": True
    })


@app.route("/api/admin/reject/<int:req_id>", methods=["POST"])
def reject_request(req_id):

    # TODO:
    # Update status REJECTED

    return jsonify({
        "success": True
    })


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
