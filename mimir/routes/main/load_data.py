""" this is a placeholder route to easily force an import 
    it will be moved to a celery task later
"""
import nntplib
import re
from datetime import datetime
from email import message_from_string
from email.header import decode_header

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

def get_attachment_filename(input: str):
    if input:
        # Using regular expression to extract filename
        match = re.search(r'filename="([^"]+)"', input)
        if match:
            return match.group(1)

    return None

def process_mime_message(msg, meta: dict):
    attachments = []
    for part in msg.walk():
        content_type = part.get_content_type()
        content_disposition = str(part.get("Content-Disposition"))

        print(f"MIME: {content_type} - {content_disposition}")
        if content_type == 'multipart/mixed':
            continue

        if "attachment" not in content_disposition:
            body = part.get_payload(decode=True).decode()
            print(f"MIME BODY: {body}")

        else:
            attachment_data = part.get_payload(decode=True).decode()
            attachment_header, _ = decode_header(content_disposition)[0]
            filename = get_attachment_filename(attachment_header)

            data = {
                'filename': filename,
                'data': attachment_data
            }
            attachments.append(data)

    # print(f"MIME: {content} | {meta}")
    article = body
    return {'article': article, 'attachments': attachments}

def save_article(n_article: dict):
    article = Article.objects(message_id=n_article['message_id']).first()

    if article is not None:
        article.update(**n_article)

    else:
        article = Article(**n_article).save()

    return article

def save_attachments(attachments_data):
    attachments = []

    for data in attachments_data:
        print(f"ATT DATA: {data}")
        d_dict = {
            'filename': None,
            'content': None
        }
        attachment = Attachment(**d_dict).save()
        attachments.append(attachment)
    return attachments

def process_and_save_message(msg: str, meta: dict):
    message = message_from_string(msg)

    # Determine message type and process accordingly
    if message.is_multipart():
        print("PROCESS: MULTIPART!")
        article_data, attachment_data = process_mime_message(msg=message, meta=meta)
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