import os
import csv
import io
import re
import hashlib
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, make_response, redirect, url_for, session
from database import db, init_db
from models import Transaction, Category, Budget, User
from functools import wraps

app = Flask(__name__)
app.secret_key = 'spendsmart-secret-2026'
init_db(app)


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session.get('user_id')


def auto_category(description):
    desc = description.lower()
    if any(x in desc for x in ['swiggy', 'zomato', 'grocery', 'food', 'restaurant', 'cafe', 'biryani', 'pizza', 'bigbasket', 'blinkit', 'bakery', 'sweet', 'hotel', 'tiffin']):
        return 'Food'
    if any(x in desc for x in ['uber', 'ola', 'petrol', 'bus', 'train', 'metro', 'rapido', 'auto', 'fuel', 'petroleum']):
        return 'Transport'
    if any(x in desc for x in ['amazon', 'flipkart', 'myntra', 'shopping', 'meesho', 'ajio', 'nykaa', 'supermart', 'mart', 'enterprises', 'store', 'shop']):
        return 'Shopping'
    if any(x in desc for x in ['netflix', 'hotstar', 'spotify', 'movie', 'prime', 'youtube', 'bookmyshow', 'sports']):
        return 'Entertainment'
    if any(x in desc for x in ['hospital', 'pharmacy', 'doctor', 'apollo', 'medplus', 'clinic', 'health', 'medical', 'homeo']):
        return 'Health'
    if any(x in desc for x in ['electricity', 'airtel', 'jio', 'bsnl', 'bill', 'water', 'apspdcl', 'bescom', 'recharge']):
        return 'Bills'
    if any(x in desc for x in ['udemy', 'school', 'college', 'course', 'fees', 'coursera', 'byju']):
        return 'Education'
    if any(x in desc for x in ['received from', 'salary', 'credited']):
        return 'Income'
    if any(x in desc for x in ['transfer to', 'paid to']):
        return 'Transfer'
    return 'Other'


def get_forecast(total_spent):
    today = date.today()
    days_elapsed = today.day
    days_in_month = 30
    days_remaining = days_in_month - days_elapsed
    if days_elapsed == 0:
        forecast = 0
    else:
        daily_rate = total_spent / days_elapsed
        forecast = round(daily_rate * days_in_month, 2)
    progress = round((days_elapsed / days_in_month) * 100)
    if forecast < 10000:
        status = 'good'
    elif forecast < 20000:
        status = 'warning'
    else:
        status = 'high'
    return forecast, progress, days_remaining, status


def get_budget_alerts(expenses, uid):
    alerts = []
    budgets = Budget.query.filter_by(user_id=uid).all()
    for b in budgets:
        cat_total = sum(t.amount for t in expenses if t.category == b.category)
        percent = round((cat_total / b.limit_amount) * 100) if b.limit_amount > 0 else 0
        if percent >= 80:
            alerts.append({
                'category': b.category,
                'spent': round(cat_total, 2),
                'limit': b.limit_amount,
                'percent': percent
            })
    return alerts


# ─── PDF BANK / PHONEPE / GPAY STATEMENT PARSER ──────────────────────────────
def parse_pdf_statement(file_stream):
    """
    Reads a PDF statement and extracts transactions.
    Supports PhonePe/GPay style line format:
        "Jun 19, 2026 Paid to LUCKY DABAGARDENS GF DEBIT ₹375"
    and falls back to structured bank statement tables.
    """
    import pdfplumber

    rows = []

    # ── PhonePe/GPay style: Date + Description + DEBIT/CREDIT + Amount on one line ──
    phonepe_pattern = re.compile(
        r'^([A-Za-z]{3}\s+\d{1,2},\s+\d{4})\s+(.+?)\s+(DEBIT|CREDIT)\s+₹([\d,]+\.?\d*)\s*$'
    )

    with pdfplumber.open(file_stream) as pdf:
        # ── Attempt 1: PhonePe/GPay line pattern ──
        for page in pdf.pages:
            text = page.extract_text() or ''
            for line in text.split('\n'):
                line = line.strip()
                m = phonepe_pattern.match(line)
                if m:
                    date_str, desc, txn_type, amount_str = m.groups()
                    try:
                        amount = float(amount_str.replace(',', ''))
                    except ValueError:
                        continue
                    if amount <= 0:
                        continue
                    rows.append({
                        'date': date_str,
                        'description': desc.strip()[:200],
                        'amount': amount,
                        'type': 'income' if txn_type == 'CREDIT' else 'expense'
                    })

        # ── Attempt 2: structured bank statement tables (if PhonePe pattern found nothing) ──
        if not rows:
            date_pattern = re.compile(r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})')
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = [str(h).strip().lower() if h else '' for h in table[0]]

                    date_idx = next((i for i, h in enumerate(header) if 'date' in h), None)
                    desc_idx = next((i for i, h in enumerate(header) if any(k in h for k in ['narration', 'description', 'particulars', 'details'])), None)
                    debit_idx = next((i for i, h in enumerate(header) if 'debit' in h or 'withdrawal' in h), None)
                    credit_idx = next((i for i, h in enumerate(header) if 'credit' in h or 'deposit' in h), None)
                    amount_idx = next((i for i, h in enumerate(header) if h == 'amount'), None)
                    type_idx = next((i for i, h in enumerate(header) if h == 'type'), None)

                    if date_idx is None or desc_idx is None:
                        continue

                    for row in table[1:]:
                        try:
                            row = [str(c).strip() if c else '' for c in row]
                            date_val = row[date_idx]
                            desc = row[desc_idx]
                            if not date_val or not desc:
                                continue
                            if not date_pattern.search(date_val):
                                continue

                            if debit_idx is not None and credit_idx is not None:
                                debit_val = row[debit_idx].replace(',', '').strip()
                                credit_val = row[credit_idx].replace(',', '').strip()
                                if credit_val and credit_val not in ('', '-', '0', '0.00'):
                                    amount = float(re.sub(r'[^\d.]', '', credit_val) or 0)
                                    ttype = 'income'
                                elif debit_val and debit_val not in ('', '-', '0', '0.00'):
                                    amount = float(re.sub(r'[^\d.]', '', debit_val) or 0)
                                    ttype = 'expense'
                                else:
                                    continue
                            elif amount_idx is not None:
                                amt_str = row[amount_idx].replace(',', '').strip()
                                amount = float(re.sub(r'[^\d.]', '', amt_str) or 0)
                                type_str = row[type_idx].lower() if type_idx is not None else 'debit'
                                ttype = 'income' if 'credit' in type_str else 'expense'
                            else:
                                continue

                            if amount <= 0:
                                continue

                            rows.append({'date': date_val, 'description': desc[:200], 'amount': amount, 'type': ttype})
                        except (ValueError, IndexError):
                            continue

    return rows


# ─── AUTH ROUTES ─────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('home'))
    error = None
    success = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not email or not password:
            error = 'All fields are required!'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters!'
        elif User.query.filter_by(username=username).first():
            error = 'Username already taken! Choose a different one.'
        elif User.query.filter_by(email=email).first():
            error = 'Email already registered! Please login instead.'
        else:
            new_user = User(username=username, email=email, password=hash_password(password))
            db.session.add(new_user)
            db.session.commit()
            success = 'Account created successfully! You can now sign in.'
    return render_template('register.html', error=error, success=success)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username, password=hash_password(password)).first()
        if user:
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('home'))
        else:
            error = 'Invalid username or password. Please try again!'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─── MAIN ROUTES ─────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def home():
    return render_template('index.html')


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        file = request.files.get('csv_file')
        if not file:
            return jsonify({'error': 'Please upload a file!'}), 400

        filename = file.filename.lower()
        uid = current_user_id()
        added = 0
        skipped = 0

        if filename.endswith('.pdf'):
            try:
                rows = parse_pdf_statement(file.stream)
            except Exception:
                return jsonify({'error': 'Could not read this PDF. Make sure it is a text-based statement, not a scanned image.'}), 400

            if not rows:
                return jsonify({'error': 'No transactions found in this PDF. Try uploading a CSV instead, or check the PDF has a clear transaction list.'}), 400

            for r in rows:
                try:
                    category = auto_category(r['description'])
                    t = Transaction(
                        user_id=uid,
                        date=r['date'],
                        description=r['description'][:200],
                        amount=r['amount'],
                        type=r['type'],
                        category=category
                    )
                    db.session.add(t)
                    added += 1
                except:
                    skipped += 1
                    continue
            db.session.commit()
            return jsonify({'success': True, 'added': added, 'skipped': skipped,
                            'message': str(added) + ' transactions imported successfully!'})

        elif filename.endswith('.csv'):
            stream = file.stream.read().decode('utf-8').splitlines()
            reader = csv.DictReader(stream)
            for row in reader:
                try:
                    date_val = row.get('Date') or row.get('date') or ''
                    desc = row.get('Description') or row.get('description') or row.get('Narration') or ''
                    amt = row.get('Amount') or row.get('amount') or '0'
                    typ = row.get('Type') or row.get('type') or 'Debit'
                    amt = float(str(amt).replace(',', '').strip())
                    if not date_val or not desc or amt <= 0:
                        skipped += 1
                        continue
                    category = auto_category(desc)
                    ttype = 'income' if 'credit' in typ.lower() else 'expense'
                    t = Transaction(user_id=uid, date=date_val.strip(), description=desc.strip(), amount=amt, type=ttype, category=category)
                    db.session.add(t)
                    added += 1
                except:
                    skipped += 1
                    continue
            db.session.commit()
            return jsonify({'success': True, 'added': added, 'skipped': skipped, 'message': str(added) + ' transactions imported successfully!'})

        else:
            return jsonify({'error': 'Please upload a CSV or PDF file!'}), 400

    return render_template('upload.html')


@app.route('/transactions')
@login_required
def transactions():
    uid = current_user_id()
    all_transactions = Transaction.query.filter_by(user_id=uid).order_by(Transaction.date.desc()).all()
    return render_template('transactions.html', transactions=all_transactions)


@app.route('/dashboard')
@login_required
def dashboard():
    from sqlalchemy import func
    uid = current_user_id()
    transactions = Transaction.query.filter_by(user_id=uid).all()
    expenses = [t for t in transactions if t.type == 'expense']
    incomes = [t for t in transactions if t.type == 'income']

    total_spent = round(sum(t.amount for t in expenses), 2)
    total_income = round(sum(t.amount for t in incomes), 2)
    savings = round(total_income - total_spent, 2)
    total_count = len(transactions)

    forecast_amount, forecast_progress, days_remaining, forecast_status = get_forecast(total_spent)
    budget_alerts = get_budget_alerts(expenses, uid)

    sorted_expenses = sorted(expenses, key=lambda t: t.amount, reverse=True)
    top3 = list(enumerate(sorted_expenses[:3], start=1))

    savings_goal = session.get('savings_goal', 0)
    if savings_goal > 0:
        goal_percent = min(round((savings / savings_goal) * 100), 100)
        remaining_goal = max(round(savings_goal - savings, 2), 0)
    else:
        goal_percent = 0
        remaining_goal = savings_goal

    cat_data = db.session.query(
        Transaction.category, func.sum(Transaction.amount), func.count(Transaction.id)
    ).filter(Transaction.type == 'expense', Transaction.user_id == uid).group_by(Transaction.category).all()

    category_data = [(c, round(a, 2), n) for c, a, n in cat_data]
    cat_labels = [c for c, a, n in category_data]
    cat_values = [a for c, a, n in category_data]

    return render_template('dashboard.html',
        total_spent=total_spent, total_income=total_income, savings=savings, total_count=total_count,
        forecast_amount=forecast_amount, forecast_progress=forecast_progress, days_remaining=days_remaining,
        forecast_status=forecast_status, budget_alerts=budget_alerts, top3=top3, savings_goal=savings_goal,
        goal_percent=goal_percent, remaining_goal=remaining_goal, category_data=category_data,
        cat_labels=cat_labels, cat_values=cat_values)


@app.route('/set-goal', methods=['POST'])
@login_required
def set_goal():
    goal = request.form.get('goal', 0)
    try:
        session['savings_goal'] = float(goal)
    except:
        session['savings_goal'] = 0
    return redirect(url_for('dashboard'))


@app.route('/budget')
@login_required
def budget_page():
    uid = current_user_id()
    budgets = Budget.query.filter_by(user_id=uid).all()
    return render_template('budget.html', budgets=budgets)


@app.route('/set-budget', methods=['POST'])
@login_required
def set_budget():
    uid = current_user_id()
    category = request.form.get('category')
    limit_amount = float(request.form.get('limit_amount', 0))
    month = datetime.now().month
    year = datetime.now().year
    existing = Budget.query.filter_by(user_id=uid, category=category, month=month, year=year).first()
    if existing:
        existing.limit_amount = limit_amount
    else:
        b = Budget(user_id=uid, category=category, limit_amount=limit_amount, month=month, year=year)
        db.session.add(b)
    db.session.commit()
    return redirect(url_for('budget_page'))


@app.route('/advisor')
@login_required
def advisor():
    from sqlalchemy import func
    uid = current_user_id()
    transactions = Transaction.query.filter_by(user_id=uid).all()
    expenses = [t for t in transactions if t.type == 'expense']
    incomes = [t for t in transactions if t.type == 'income']

    total_spent = round(sum(t.amount for t in expenses), 2)
    total_income = round(sum(t.amount for t in incomes), 2)
    savings = round(total_income - total_spent, 2)
    total_count = len(transactions)

    cat_data = db.session.query(
        Transaction.category, func.sum(Transaction.amount), func.count(Transaction.id)
    ).filter(Transaction.type == 'expense', Transaction.user_id == uid).group_by(Transaction.category).all()
    categories = [[c, round(a, 2), n] for c, a, n in cat_data]

    sorted_expenses = sorted(expenses, key=lambda t: t.amount, reverse=True)
    top3 = [{'description': t.description, 'amount': t.amount, 'category': t.category} for t in sorted_expenses[:3]]

    spending_summary = {'total_income': total_income, 'total_spent': total_spent, 'savings': savings,
                        'total_count': total_count, 'categories': categories, 'top3': top3}
    return render_template('advisor.html', spending_summary=spending_summary)


@app.route('/api/ask-advisor', methods=['POST'])
@login_required
def ask_advisor():
    data = request.get_json()
    question = data.get('question', '').lower()
    summary = data.get('summary', {})

    total_spent = summary.get('total_spent', 0)
    total_income = summary.get('total_income', 0)
    savings = summary.get('savings', 0)
    categories = summary.get('categories', [])
    top3 = summary.get('top3', [])

    sorted_cats = sorted(categories, key=lambda x: x[1], reverse=True)
    top_cat = sorted_cats[0] if sorted_cats else ['Unknown', 0, 0]
    low_cat = sorted_cats[-1] if sorted_cats else ['Unknown', 0, 0]
    savings_pct = round((savings / total_income) * 100) if total_income > 0 else 0

    if any(w in question for w in ['overspend', 'too much', 'most', 'highest']):
        pct = round(top_cat[1] / total_spent * 100) if total_spent else 0
        reply = ("Your biggest spending category is **" + str(top_cat[0]) + "** at Rs." + str(top_cat[1]) + " (" + str(pct) + "% of total expenses).\n\n"
            "- You made " + str(top_cat[2]) + " transactions in this category\n"
            "- Try setting a monthly budget limit for " + str(top_cat[0]) + "\n"
            "- Cutting " + str(top_cat[0]) + " by 20% would save you Rs." + str(round(top_cat[1] * 0.2)) + " this month")
    elif any(w in question for w in ['save', 'saving', 'savings']):
        tip = "Great job! You are saving well!" if savings_pct >= 20 else "Aim for 20% savings. You need Rs." + str(round(total_income * 0.2 - savings)) + " more."
        reply = ("You saved Rs." + str(savings) + " this month — " + str(savings_pct) + "% of your income.\n\n"
            "- " + tip + "\n"
            "- Cut " + str(top_cat[0]) + " by 30% to free Rs." + str(round(top_cat[1] * 0.3)) + "\n"
            "- Try the 50-30-20 rule: 50% needs, 30% wants, 20% savings")
    elif any(w in question for w in ['cut', 'reduce', 'which category']):
        reply = ("Cut down on **" + str(top_cat[0]) + "** first — highest spend at Rs." + str(top_cat[1]) + ".\n\n"
            "- Target Rs." + str(round(top_cat[1] * 0.7)) + " to save Rs." + str(round(top_cat[1] * 0.3)) + "\n"
            "- Lowest category: " + str(low_cat[0]) + " at Rs." + str(low_cat[1]) + " — keep it!\n"
            "- Set a strict budget limit on the Budget page")
    elif any(w in question for w in ['budget', 'plan', 'monthly']):
        reply = "Suggested budget plan based on Rs." + str(total_income) + " income:\n\n"
        for cat in sorted_cats[:5]:
            reply += "- **" + str(cat[0]) + "**: Rs." + str(cat[1]) + " → Rs." + str(round(cat[1] * 0.8)) + "\n"
        reply += "\nThis saves you Rs." + str(round(total_spent * 0.2)) + " extra per month!"
    elif any(w in question for w in ['mistake', 'wrong', 'bad']):
        mistakes = []
        if savings_pct < 20:
            mistakes.append("Low savings — only " + str(savings_pct) + "% (target 20%+)")
        if total_spent > 0 and top_cat[1] > total_spent * 0.4:
            mistakes.append(str(top_cat[0]) + " is " + str(round(top_cat[1]/total_spent*100)) + "% of spending — too high!")
        if top3:
            mistakes.append("Big single purchase: " + str(top3[0]['description']) + " Rs." + str(top3[0]['amount']))
        if not mistakes:
            mistakes.append("Your spending looks balanced this month!")
        reply = "Top financial issues:\n\n"
        for i, m in enumerate(mistakes, 1):
            reply += str(i) + ". " + m + "\n"
    elif any(w in question for w in ['food', 'eating', 'swiggy', 'zomato']):
        food = next((c for c in categories if c[0] == 'Food'), None)
        if food and food[2] > 0:
            reply = ("Food spending: Rs." + str(food[1]) + " across " + str(food[2]) + " orders.\n\n"
                "- Rs." + str(round(food[1]/food[2])) + " per order average\n"
                "- Cooking home 3 days/week saves Rs." + str(round(food[1]*0.3)) + " monthly")
        else:
            reply = "No food transactions found. Upload your bank statement first!"
    else:
        reply = ("Your financial snapshot:\n\n"
            "- Income: Rs." + str(total_income) + " | Spent: Rs." + str(total_spent) + " | Saved: Rs." + str(savings) + "\n"
            "- Savings rate: " + str(savings_pct) + "% " + ("(Great!)" if savings_pct >= 20 else "(Aim for 20%)") + "\n"
            "- Biggest spend: " + str(top_cat[0]) + " Rs." + str(top_cat[1]) + "\n\n"
            "Ask me 'where am I overspending?' or 'make me a budget plan'!")

    return jsonify({'reply': reply})


@app.route('/export/pdf')
@login_required
def export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    uid = current_user_id()
    transactions = Transaction.query.filter_by(user_id=uid).order_by(Transaction.date.desc()).all()
    expenses = [t for t in transactions if t.type == 'expense']
    incomes = [t for t in transactions if t.type == 'income']
    total_spent = round(sum(t.amount for t in expenses), 2)
    total_income = round(sum(t.amount for t in incomes), 2)
    savings = round(total_income - total_spent, 2)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph("<b>SpendSmart — Financial Audit Report</b>", styles['Title']))
    elements.append(Spacer(1, 20))
    summary_data = [['Total Income', 'Total Spent', 'Savings', 'Transactions'],
        ['Rs.'+str(total_income), 'Rs.'+str(total_spent), 'Rs.'+str(savings), str(len(transactions))]]
    summary_table = Table(summary_data, colWidths=[120,120,120,120])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#667eea')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,-1),11),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('BACKGROUND',(0,1),(-1,1),colors.HexColor('#f0f0ff')),
        ('GRID',(0,0),(-1,-1),0.5,colors.grey),
        ('PADDING',(0,0),(-1,-1),10),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1,30))
    elements.append(Paragraph("<b>All Transactions</b>", styles['Heading2']))
    elements.append(Spacer(1,10))
    data = [['Date','Description','Category','Type','Amount']]
    for t in transactions:
        typ = 'Credit' if t.type == 'income' else 'Debit'
        amt = '+Rs.'+str(t.amount) if t.type == 'income' else '-Rs.'+str(t.amount)
        data.append([t.date, t.description[:35], t.category, typ, amt])
    table = Table(data, colWidths=[80,180,80,60,80])
    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#667eea')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('FONTSIZE',(0,0),(-1,-1),9),
        ('GRID',(0,0),(-1,-1),0.3,colors.grey),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f8f8ff')]),
        ('PADDING',(0,0),(-1,-1),7),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'attachment; filename=spendsmart_report.pdf'
    return response


@app.route('/export/csv')
@login_required
def export_csv():
    uid = current_user_id()
    transactions = Transaction.query.filter_by(user_id=uid).order_by(Transaction.date.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date','Description','Category','Type','Amount'])
    for t in transactions:
        writer.writerow([t.date, t.description, t.category, t.type, t.amount])
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=spendsmart_report.csv'
    return response


@app.route('/neelima')
def neelima():
    return app.send_static_file('SpendSmart.html')


if __name__ == '__main__':
    app.run(debug=True)
