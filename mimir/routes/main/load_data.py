""" this is a placeholder route to easily force an import 
    it will be moved to a celery task later
"""
import nntplib
from datetime import datetime
from email import policy
from email.parser import BytesParser

from mimir.models import Article
from mimir.routes.main import bp_main

nntp_server = 'nntp.lore.kernel.org'
nntp_group = 'org.kernel.vger.linux-kernel'

s = nntplib.NNTP(nntp_server)


@bp_main.route('/import/')
def import_page() -> str:
    resp, count, first, last, name = s.group(nntp_group)
    resp, overviews = s.over((first, first + 50))

    for art_id, over in overviews:
        # print(art_id, nntplib.decode_header(over['subject']))
        mail_from = nntplib.decode_header(over['from'])
        date_raw = nntplib.decode_header(over['date'])
        date = datetime.strptime(date_raw, '%a, %d %b %Y %H:%M:%S %z')
        message_id = nntplib.decode_header(over['message-id'])
        references = nntplib.decode_header(over['references'])
        subject = nntplib.decode_header(over['subject'])

        resp, number, message_id = s.stat(art_id)
        resp, article = s.article(message_id)
        article_raw = b'\n'.join(article.lines)

        parser = BytesParser(policy=policy.default)
        article = parser.parsebytes(article_raw)

        # Decode the article content based on the specified encoding
        encoding = article.get_content_charset()
        if encoding:
            article_payload = article.get_payload(decode=True).decode(encoding)
        else:
            # Use a default encoding if not specified
            article_payload = article.get_payload(decode=True).decode('utf-8', 'ignore')

        article_data = {
            'author': mail_from,
            'date': date,
            'message_id': message_id,
            'references': references,
            'subject': subject,
            'body': article_payload
        }
        article = Article.objects(message_id=message_id).first()
        if article is not None:
            article.update(**article_data)
        else:
            article = Article(**article_data).save()

        print(article.message_id)

    s.quit()
    return 'Hallo'