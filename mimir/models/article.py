from datetime import datetime
from mimir.extensions import db

class Article(db.Document):
    subject = db.StringField()
    body = db.StringField()
    author = db.StringField()
    references = db.StringField()
    message_id = db.StringField(unique=True)
    srv_id = db.IntField(unique=True)
    date = db.DateTimeField(default=datetime.utcnow)
