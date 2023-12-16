""" this is a placeholder route to easily force an import 
    it will be moved to a celery task later
"""
import nntplib
from datetime import datetime
from email import message_from_string

from mimir.models import Article, Attachment
from mimir.routes.main import bp_main

nntp_server = 'nntp.lore.kernel.org'
nntp_group = 'org.kernel.vger.linux-kernel'

s = nntplib.NNTP(nntp_server)

def process_plain_text(msg, meta: dict):
    # get the body-less metadata, add body key to it
    # eventually this will probably need to do more
    full_article = meta
    full_article['body'] = msg.get_payload(decode=True).decode()
    return full_article

def process_mime_message(message_data):
    return ()

def save_article(n_article: dict):
    article = Article.objects(message_id=n_article['message_id']).first()

    if article is not None:
        article.update(**n_article)

    else:
        article = Article(**n_article).save()

    return article

def save_attachments(attachments_data):
    attachments = []
    for attachment_data in attachments_data:
        attachment = Attachment(**attachment_data).save()
        attachments.append(attachment)
    return attachments

def process_and_save_message(msg: str, meta: dict):
    message = message_from_string(msg)

    # Determine message type and process accordingly
    if message.is_multipart():
        print("PROCESS: MULTIPART!")
        article_data, attachment_data = process_mime_message(message)
        attachments = save_attachments(attachment_data)
        article_data['attachment'] = attachments

    else:
        article_data = process_plain_text(msg=message, meta=meta)

    # Save or update the Article model
    save_article(article_data)


@bp_main.route('/import/')
def import_page() -> str:
    resp, count, first, last, name = s.group(nntp_group)
    resp, overviews = s.over((first, first + 50))

    for art_id, over in overviews:
        print(f"================= {art_id} ====================")
        try:
            # print(art_id, nntplib.decode_header(over['subject']))
            mail_from = nntplib.decode_header(over['from'])
            date_raw = nntplib.decode_header(over['date'])
            date = datetime.strptime(date_raw, '%a, %d %b %Y %H:%M:%S %z')
            message_id = nntplib.decode_header(over['message-id'])
            references = nntplib.decode_header(over['references'])
            subject = nntplib.decode_header(over['subject'])

        except Exception as error:
            print(f"ERROR: {error}")

        else:
            resp, number, message_id = s.stat(art_id)
            resp, article = s.article(message_id)
            article_bin = b'\n'.join(article.lines)
            article_dec = article_bin.decode()
            if art_id == 36 or art_id == 37:
                print(f"BIN: {article_dec}")

            article_meta = {
                'author': mail_from,
                'date': date,
                'message_id': message_id,
                'srv_id': art_id,
                'references': references,
                'subject': subject,
            }

            process_and_save_message(
                msg=article_dec,
                meta=article_meta
            )

    s.quit()
    return 'Hallo'