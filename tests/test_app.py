import sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from app import app,init_db,db
@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as t:
        app.config.update(TESTING=True,DATABASE=str(Path(t)/'test.db'))
        init_db()
        with app.test_client() as c:yield c

def token(c):
    with c.session_transaction() as s:return s['_csrf']
def test_home_and_search(client):
    assert client.get('/').status_code==200
    assert b'Coastal Express' in client.get('/trains?origin=Dhaka').data

def test_auth_booking_payment_pdf_and_qr(client):
    client.get('/register');r=client.post('/register',data={'_csrf':token(client),'name':'Test User','email':'test@example.com','password':'password123'},follow_redirects=True);assert r.status_code==200
    client.get('/login');client.post('/login',data={'_csrf':token(client),'email':'test@example.com','password':'password123'})
    import datetime
    date=(datetime.date.today()+datetime.timedelta(days=1)).isoformat()
    r=client.post('/book/1',data={'_csrf':token(client),'date':date,'seat':'1'});assert r.status_code==302
    ref=r.location.rsplit('/',1)[-1]
    assert client.post('/book/1',data={'_csrf':token(client),'date':date,'seat':'1'},follow_redirects=True).status_code==200
    assert client.post('/checkout/'+ref,data={'_csrf':token(client)}).status_code==302
    assert client.get('/ticket/'+ref+'/pdf').data.startswith(b'%PDF')
    assert b'Demo' in client.get('/verify/'+ref).data

def test_admin_restricted(client):
    assert client.get('/admin').status_code==302
