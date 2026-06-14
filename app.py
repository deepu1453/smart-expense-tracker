import os
import csv
from flask import Flask, render_template, request, jsonify
from database import db, init_db
from models import Transaction, Category, Budget

app = Flask(__name__)
init_db(app)

# Auto categorization function
def auto_category(description):
    desc = description.lower()
    if any(x in desc for x in ['swiggy','zomato','grocery','food','restaurant','cafe','biryani','pizza']):
        return 'Food'
    if any(x in desc for x in ['uber','ola','petrol','bus','train','metro','rapido','auto']):
        return 'Transport'
    if any(x in desc for x in ['amazon','flipkart','myntra','shopping','meesho','ajio']):
        return 'Shopping'
    if any(x in desc for x in ['netflix','hotstar','spotify','movie','prime','youtube']):
        return 'Entertainment'
    if any(x in desc for x in ['hospital','pharmacy','doctor','apollo','medplus','clinic']):
        return 'Health'
    if any(x in desc for x in ['electricity','airtel','jio','bsnl','bill','water','apspdcl']):
        return 'Bills'
    if any(x in desc for x in ['udemy','school','college','course','fees','coursera']):
        return 'Education'
    return 'Other'

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/upload', methods=['GET','POST'])
def upload():
    if request.method == 'POST':
        file = request.files.get('csv_file')
        if not file or not file.filename.endswith('.csv'):
            return jsonify({'error': 'Please upload a valid CSV file!'}), 400
        added = 0
        skipped = 0
        stream = file.stream.read().decode('utf-8').splitlines()
        reader = csv.DictReader(stream)
        for row in reader:
            try:
                # Handle different column name formats
                date = row.get('Date') or row.get('date') or ''
                desc = row.get('Description') or row.get('description') or row.get('Narration') or ''
                amt  = row.get('Amount') or row.get('amount') or '0'
                typ  = row.get('Type') or row.get('type') or 'Debit'
                amt  = float(str(amt).replace(',','').strip())
                if not date or not desc or amt <= 0:
                    skipped += 1
                    continue
                category = auto_category(desc)
                ttype = 'income' if 'credit' in typ.lower() else 'expense'
                t = Transaction(
                    date=date.strip(),
                    description=desc.strip(),
                    amount=amt,
                    type=ttype,
                    category=category
                )
                db.session.add(t)
                added += 1
            except:
                skipped += 1
                continue
        db.session.commit()
        return jsonify({
            'success': True,
            'added': added,
            'skipped': skipped,
            'message': f'{added} transactions imported successfully!'
        })
    return render_template('upload.html')

@app.route('/transactions')
def transactions():
    all_transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    return render_template('transactions.html', transactions=all_transactions)

@app.route('/api/transactions')
def api_transactions():
    all_transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    return jsonify([{
        'id': t.id,
        'date': t.date,
        'description': t.description,
        'amount': t.amount,
        'type': t.type,
        'category': t.category
    } for t in all_transactions])
@app.route('/dashboard')
def dashboard():
    from sqlalchemy import func
    transactions = Transaction.query.all()
    expenses = [t for t in transactions if t.type == 'expense']
    incomes = [t for t in transactions if t.type == 'income']
    total_spent = round(sum(t.amount for t in expenses), 2)
    total_income = round(sum(t.amount for t in incomes), 2)
    savings = round(total_income - total_spent, 2)
    total_count = len(transactions)
    cat_data = db.session.query(
        Transaction.category,
        func.sum(Transaction.amount),
        func.count(Transaction.id)
    ).filter(Transaction.type == 'expense').group_by(Transaction.category).all()
    category_data = [(c, round(a, 2), n) for c, a, n in cat_data]
    cat_labels = [c for c, a, n in category_data]
    cat_values = [a for c, a, n in category_data]
    return render_template('dashboard.html',
        total_spent=total_spent,
        total_income=total_income,
        savings=savings,
        total_count=total_count,
        category_data=category_data,
        cat_labels=cat_labels,
        cat_values=cat_values
    )
@app.route('/export/pdf')
def export_pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from flask import make_response
    import io

    transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    expenses = [t for t in transactions if t.type == 'expense']
    incomes = [t for t in transactions if t.type == 'income']
    total_spent = round(sum(t.amount for t in expenses), 2)
    total_income = round(sum(t.amount for t in incomes), 2)
    savings = round(total_income - total_spent, 2)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title = Paragraph("<b>Smart Expense Tracker — Audit Report</b>", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 20))

    # Summary
    summary_data = [
        ['Total Income', 'Total Spent', 'Savings', 'Transactions'],
        [f'Rs.{total_income}', f'Rs.{total_spent}', f'Rs.{savings}', str(len(transactions))]
    ]
    summary_table = Table(summary_data, colWidths=[120, 120, 120, 120])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#667eea')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 11),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('BACKGROUND', (0,1), (-1,1), colors.HexColor('#f0f0ff')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#f0f0ff')]),
        ('PADDING', (0,0), (-1,-1), 10),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 30))

    # Transactions table
    heading = Paragraph("<b>All Transactions</b>", styles['Heading2'])
    elements.append(heading)
    elements.append(Spacer(1, 10))

    data = [['Date', 'Description', 'Category', 'Type', 'Amount']]
    for t in transactions:
        typ = 'Credit' if t.type == 'income' else 'Debit'
        amt = f'+Rs.{t.amount}' if t.type == 'income' else f'-Rs.{t.amount}'
        data.append([t.date, t.description[:35], t.category, typ, amt])

    table = Table(data, colWidths=[80, 180, 80, 60, 80])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#667eea')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('ALIGN', (4,0), (4,-1), 'RIGHT'),
        ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8f8ff')]),
        ('PADDING', (0,0), (-1,-1), 7),
    ]))
    elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'attachment; filename=expense_report.pdf'
    return response

@app.route('/export/csv')
def export_csv():
    import csv
    import io
    from flask import make_response
    transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Description', 'Category', 'Type', 'Amount'])
    for t in transactions:
        writer.writerow([t.date, t.description, t.category, t.type, t.amount])
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=expense_report.csv'
    return response
if __name__ == '__main__':
    app.run(debug=True)