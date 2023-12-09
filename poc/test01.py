import nntplib

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
    print(art_id, nntplib.decode_header(over['subject']))
    if art_id == 10:
        resp, number, message_id = s.stat(art_id)
        resp, info = s.article(message_id)
        print(info)

s.quit()
