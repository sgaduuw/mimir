import nntplib
from datetime import datetime

from email import policy
from email.parser import BytesParser

from mimir.config import Config
from mimir.models import Article

nntp_server = 'nntp.lore.kernel.org'
nntp_group = 'org.kernel.vger.linux-kernel'

s = nntplib.NNTP(nntp_server)

print(f"Description: {s.description(nntp_group)}")

# Send a GROUP command, where name is the group name.
# The group is selected as the current group, if it exists.
# Returns a tuple (response, count, first, last, name) where count is the
# (estimated) number of articles in the group, first is the first article
# number in the group, last is the last article number in the group, and
# name is the group name.
resp, count, first, last, name = s.group('org.kernel.vger.linux-kernel')
print('Group', name, 'has', count, 'articles, range', first, 'to', last)

resp, overviews = s.over((first, first + 9))
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

    # print(article_payload)
    print('============================================================')

    # article = {
    #     'author': mail_from,
    #     # 'date': date,
    #     'message_id': message_id,
    #     'references': references,
    #     'subject': subject
    # }
    # Article(**article).save()

s.quit()
