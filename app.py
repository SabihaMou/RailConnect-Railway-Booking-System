import os, sqlite3, secrets, io, datetime, functools
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.pdfgen import canvas
import qrcode

BASE=Path(__file__).resolve().parent
app=Flask(__name__)
app.secret_key=os.environ.get('RAILCONNECT_SECRET',secrets.token_hex(32))
app.config['DATABASE']=os.environ.get('RAILCONNECT_DB',str(BASE/'instance'/'railconnect.sqlite3'))
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',MAX_CONTENT_LENGTH=1024*1024)

def db():
    d=sqlite3.connect(app.config['DATABASE'],timeout=15,isolation_level=None)
    d.row_factory=sqlite3.Row;d.execute('PRAGMA foreign_keys=ON');d.execute('PRAGMA busy_timeout=5000');return d

def init_db():
    Path(app.config['DATABASE']).parent.mkdir(parents=True,exist_ok=True)
    with db() as d:
        d.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'passenger');
        CREATE TABLE IF NOT EXISTS trains(id INTEGER PRIMARY KEY,name TEXT NOT NULL,code TEXT UNIQUE NOT NULL,origin TEXT NOT NULL,destination TEXT NOT NULL,depart TEXT NOT NULL,arrive TEXT NOT NULL,price INTEGER NOT NULL CHECK(price>0),seats INTEGER NOT NULL CHECK(seats>0),active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),train_id INTEGER NOT NULL REFERENCES trains(id),travel_date TEXT NOT NULL,seat INTEGER NOT NULL,reference TEXT UNIQUE NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(train_id,travel_date,seat));""")
        if not d.execute('SELECT 1 FROM trains').fetchone():
            d.executemany('INSERT INTO trains(name,code,origin,destination,depart,arrive,price,seats) VALUES(?,?,?,?,?,?,?,?)',[
            ('Coastal Express','RC101','Dhaka','Chattogram','07:30','13:15',850,36),('Sunrise Intercity','RC102','Chattogram','Dhaka','08:10','14:20',920,36),('Green Valley','RC103','Dhaka','Sylhet','06:45','12:30',740,36),('Royal Bengal','RC104','Dhaka','Rajshahi','09:15','15:45',790,36),('Bay Breeze','RC105','Chattogram','Cox’s Bazar','10:20','13:45',520,36),('North Star','RC106','Rajshahi','Dhaka','16:00','22:20',780,36)])
        if not d.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            password=os.environ.get('RAILCONNECT_ADMIN_PASSWORD','ChangeMe123!')
            d.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',('Demo Administrator','admin@railconnect.local',generate_password_hash(password),'admin'))

def csrf():
    if '_csrf' not in session:session['_csrf']=secrets.token_urlsafe(32)
    return session['_csrf']
app.jinja_env.globals['csrf_token']=csrf
@app.before_request
def protect():
    if request.method=='POST' and not secrets.compare_digest(request.form.get('_csrf',''),session.get('_csrf','')):abort(400,'Invalid security token; refresh and retry.')
@app.context_processor
def common():return {'today':datetime.date.today().isoformat(),'current_user':session.get('name'),'is_admin':session.get('role')=='admin'}
def login_required(f):
    @functools.wraps(f)
    def inner(*a,**kw):
        if not session.get('uid'):flash('Please sign in to continue.','warning');return redirect(url_for('login'))
        return f(*a,**kw)
    return inner
def admin_required(f):
    @functools.wraps(f)
    @login_required
    def inner(*a,**kw):
        if session.get('role')!='admin':abort(403)
        return f(*a,**kw)
    return inner
@app.route('/')
def home():
    with db() as d: trains=d.execute('SELECT * FROM trains WHERE active=1 LIMIT 3').fetchall()
    return render_template('home.html',trains=trains)
@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form.get('name','').strip()[:80];email=request.form.get('email','').strip().lower()[:150];password=request.form.get('password','')
        if len(name)<2 or '@' not in email or len(password)<8:flash('Enter a name, valid email and password of at least 8 characters.','danger')
        else:
            try:
                with db() as d:d.execute('INSERT INTO users(name,email,password) VALUES(?,?,?)',(name,email,generate_password_hash(password)))
                flash('Account created. Sign in to book your journey.','success');return redirect(url_for('login'))
            except sqlite3.IntegrityError:flash('Email already registered.','danger')
    return render_template('auth.html',register=True)
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        with db() as d:u=d.execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').strip().lower(),)).fetchone()
        if u and check_password_hash(u['password'],request.form.get('password','')):
            session.clear();session.update(uid=u['id'],name=u['name'],role=u['role']);flash('Welcome aboard! 🚆','success');return redirect(url_for('home'))
        flash('Incorrect email or password.','danger')
    return render_template('auth.html',register=False)
@app.post('/logout')
def logout():session.clear();return redirect(url_for('home'))
@app.route('/trains')
def trains():
    origin=request.args.get('origin','').strip();destination=request.args.get('destination','').strip();date=request.args.get('date',datetime.date.today().isoformat())
    if date<datetime.date.today().isoformat():date=datetime.date.today().isoformat()
    with db() as d:
        rows=d.execute('SELECT * FROM trains WHERE active=1 AND origin LIKE ? AND destination LIKE ? ORDER BY depart',('%'+origin+'%','%'+destination+'%')).fetchall()
        results=[dict(t,available=t['seats']-d.execute('SELECT COUNT(*) FROM bookings WHERE train_id=? AND travel_date=?',(t['id'],date)).fetchone()[0]) for t in rows]
    return render_template('trains.html',trains=results,origin=origin,destination=destination,date=date)
@app.route('/book/<int:train_id>',methods=['GET','POST'])
@login_required
def book(train_id):
    date=request.values.get('date',datetime.date.today().isoformat())
    try:datetime.date.fromisoformat(date)
    except ValueError:abort(400)
    if date<datetime.date.today().isoformat():abort(400,'Choose a future date')
    with db() as d:
        t=d.execute('SELECT * FROM trains WHERE id=? AND active=1',(train_id,)).fetchone()
        if not t:abort(404)
        if request.method=='POST':
            try:seat=int(request.form.get('seat','0'))
            except ValueError:seat=0
            if not 1<=seat<=t['seats']:flash('Select a valid seat.','danger')
            else:
                try:
                    d.execute('BEGIN IMMEDIATE')
                    ref='RC-'+secrets.token_hex(5).upper()
                    d.execute('INSERT INTO bookings(user_id,train_id,travel_date,seat,reference) VALUES(?,?,?,?,?)',(session['uid'],train_id,date,seat,ref))
                    d.commit();return redirect(url_for('checkout',ref=ref))
                except sqlite3.IntegrityError:d.rollback();flash('That seat was just taken. Please choose another.','warning')
        occupied={r[0] for r in d.execute('SELECT seat FROM bookings WHERE train_id=? AND travel_date=?',(train_id,date))}
    return render_template('book.html',train=t,date=date,occupied=occupied)
@app.route('/checkout/<ref>',methods=['GET','POST'])
@login_required
def checkout(ref):
    with db() as d:
        b=d.execute('SELECT b.*,t.name,t.origin,t.destination,t.depart,t.arrive,t.price FROM bookings b JOIN trains t ON b.train_id=t.id WHERE b.reference=? AND b.user_id=?',(ref,session['uid'])).fetchone()
        if not b:abort(404)
        if request.method=='POST':
            if b['status']=='pending':
                d.execute("UPDATE bookings SET status='paid' WHERE id=? AND status='pending'",(b['id'],));flash('Demo payment successful! Your ticket is ready.','success')
            return redirect(url_for('ticket',ref=ref))
    return render_template('checkout.html',b=b)
@app.route('/ticket/<ref>')
@login_required
def ticket(ref):
    with db() as d:b=d.execute('SELECT b.*,t.name,t.origin,t.destination,t.depart,t.arrive,t.price FROM bookings b JOIN trains t ON b.train_id=t.id WHERE b.reference=? AND (b.user_id=? OR ?=1)',(ref,session['uid'],int(session.get('role')=='admin'))).fetchone()
    if not b:abort(404)
    return render_template('ticket.html',b=b)
@app.route('/ticket/<ref>/pdf')
@login_required
def pdf(ref):
    with db() as d:b=d.execute("SELECT b.*,t.name,t.origin,t.destination,t.depart,t.price FROM bookings b JOIN trains t ON b.train_id=t.id WHERE b.reference=? AND (b.user_id=? OR ?=1) AND b.status='paid'",(ref,session['uid'],int(session.get('role')=='admin'))).fetchone()
    if not b:abort(404)
    output=io.BytesIO();c=canvas.Canvas(output,pagesize=(595,420));c.setFillColorRGB(.09,.12,.24);c.rect(0,0,595,420,fill=1,stroke=0);c.setFillColorRGB(1,1,1)
    c.setFont('Helvetica-Bold',25);c.drawString(35,365,'RAILCONNECT  /  E-TICKET');c.setFont('Helvetica',12)
    for i,line in enumerate([f"Reference: {b['reference']}",f"Train: {b['name']}",f"Route: {b['origin']} to {b['destination']}",f"Date: {b['travel_date']}  |  Departure: {b['depart']}",f"Seat: {b['seat']}  |  Fare: BDT {b['price']}","Payment: SIMULATED - portfolio demonstration"]):c.drawString(36,310-i*35,line)
    qr=qrcode.make(url_for('verify',ref=ref,_external=True));qrbuf=io.BytesIO();qr.save(qrbuf,format='PNG');qrbuf.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qrbuf),425,135,125,125);c.setFont('Helvetica',9);c.drawString(35,32,'Demo service. Not valid for real-world travel.');c.save();output.seek(0)
    return send_file(output,mimetype='application/pdf',as_attachment=True,download_name=f'{ref}.pdf')
@app.route('/verify/<ref>')
def verify(ref):
    with db() as d:b=d.execute('SELECT b.reference,b.status,b.travel_date,b.seat,t.name,t.origin,t.destination FROM bookings b JOIN trains t ON t.id=b.train_id WHERE b.reference=?',(ref,)).fetchone()
    return render_template('verify.html',b=b)
@app.route('/my-bookings')
@login_required
def my_bookings():
    with db() as d:rows=d.execute('SELECT b.*,t.name,t.origin,t.destination FROM bookings b JOIN trains t ON t.id=b.train_id WHERE b.user_id=? ORDER BY b.id DESC',(session['uid'],)).fetchall()
    return render_template('my_bookings.html',rows=rows)
@app.post('/cancel/<ref>')
@login_required
def cancel(ref):
    with db() as d:
        b=d.execute('SELECT id,status FROM bookings WHERE reference=? AND user_id=?',(ref,session['uid'])).fetchone()
        if not b:abort(404)
        if b['status']=='pending':d.execute('DELETE FROM bookings WHERE id=?',(b['id'],));flash('Reservation released.','success')
        else:flash('Paid demo tickets cannot be cancelled automatically.','warning')
    return redirect(url_for('my_bookings'))
@app.route('/admin')
@admin_required
def admin():
    with db() as d:
        trains=d.execute('SELECT * FROM trains ORDER BY id DESC').fetchall();bookings=d.execute('SELECT b.*,u.name AS passenger,t.name AS train FROM bookings b JOIN users u ON u.id=b.user_id JOIN trains t ON t.id=b.train_id ORDER BY b.id DESC LIMIT 30').fetchall()
        stats={'passengers':d.execute("SELECT COUNT(*) FROM users WHERE role='passenger'").fetchone()[0],'bookings':d.execute('SELECT COUNT(*) FROM bookings').fetchone()[0],'revenue':d.execute("SELECT COALESCE(SUM(t.price),0) FROM bookings b JOIN trains t ON t.id=b.train_id WHERE b.status='paid'").fetchone()[0]}
    return render_template('admin.html',trains=trains,bookings=bookings,stats=stats)
@app.post('/admin/train')
@admin_required
def add_train():
    f=request.form
    try:
        with db() as d:d.execute('INSERT INTO trains(name,code,origin,destination,depart,arrive,price,seats) VALUES(?,?,?,?,?,?,?,?)',(f['name'].strip(),f['code'].strip().upper(),f['origin'].strip(),f['destination'].strip(),f['depart'],f['arrive'],int(f['price']),int(f['seats'])))
        flash('Train added!','success')
    except (ValueError,sqlite3.IntegrityError,KeyError):flash('Could not add train. Check unique code, seats and fare.','danger')
    return redirect(url_for('admin'))
@app.post('/admin/train/<int:tid>/toggle')
@admin_required
def toggle_train(tid):
    with db() as d:d.execute('UPDATE trains SET active=1-active WHERE id=?',(tid,))
    return redirect(url_for('admin'))
if __name__=='__main__':
    init_db();print('RailConnect: http://127.0.0.1:5000');app.run(host='127.0.0.1',port=5000,debug=False)
