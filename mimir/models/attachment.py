from mimir.extensions import db

class Attachment(db.Document):
    filename = db.StringField()
    content = db.StringField()
