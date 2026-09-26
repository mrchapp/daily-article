#!/usr/bin/env python3
# Public domain; Mr.Z-man, MZMcBride; 2012
# Taken over 2026 by mrchapp, Daniel Díaz

import argparse
import datetime
from email.header import Header
from email.mime.nonmultipart import MIMENonMultipart
import html.entities
import http.cookiejar
import json
import re
import smtplib
import sys
import textwrap
import traceback
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

import config

def parse_date(text):
    """Turn a YYYY-MM-DD command-line value into a date."""
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError('expected a date like 2026-09-25, got %r' % text)

parser = argparse.ArgumentParser(
    description='Build the daily Wikipedia, Wiktionary and Wikiquote email.')
parser.add_argument('--debug', action='store_true',
                    help='print the sections, and any traceback, instead of sending or posting')
parser.add_argument('--date', type=parse_date, default=datetime.date.today(), metavar='YYYY-MM-DD',
                    help='the date to build the email for (default: today, local time)')
parser.add_argument('--output', metavar='FILE.EML',
                    help='write the email to FILE.EML instead of sending it')
parser.add_argument('--force', action='store_true',
                    help='warn on stderr and carry on when the word-of-the-day definitions cannot '
                         'be found, instead of failing')
args = parser.parse_args()

DEBUG_MODE = args.debug

# The Wikimedia API requires a descriptive User-Agent.
USER_AGENT = 'daily-article bot (https://meta.wikimedia.org/wiki/Talk:daily-article-l)'

# Establish a few wikis
metawiki_base = 'https://meta.wikimedia.org'
enwiki_base = 'https://en.wikipedia.org'
enwikt_base = 'https://en.wiktionary.org'
enquote_base = 'https://en.wikiquote.org'


class APIError(Exception):
    pass


class NoPage(Exception):
    pass


class Wiki(object):
    """Minimal replacement for the wikitools.Wiki class.

    Only the calls this bot makes are implemented. No maxlag parameter is
    sent: the old code called setMaxlag(-1), which disabled it.
    """

    def __init__(self, baseurl):
        self.baseurl = baseurl
        self.api_path = baseurl + '/w/api.php'
        # A cookie jar keeps the session that login() establishes.
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def api_request(self, params):
        params = dict(params)
        # wikitools always forced the JSON format (api.APIRequest.__init__);
        # without it the API answers with HTML-wrapped JSON.
        params['format'] = 'json'
        data = urllib.parse.urlencode(params).encode('utf-8')
        request = urllib.request.Request(
            self.api_path, data=data, headers={'User-Agent': USER_AGENT})
        with self.opener.open(request) as response:
            result = json.load(response)
        if 'error' in result:
            error = result['error']
            raise APIError('%s: %s' % (error.get('code'), error.get('info')))
        return result

    def get_wikitext(self, title):
        result = self.api_request({'action': 'query',
                                   'prop': 'revisions',
                                   'rvprop': 'content',
                                   'rvslots': 'main',
                                   'titles': title,
                                   # wikitools.Page() defaulted to followRedir=True.
                                   'redirects': ''})
        page = list(result['query']['pages'].values())[0]
        if 'missing' in page:
            raise NoPage(title)
        revision = page['revisions'][0]
        if 'slots' in revision:
            return revision['slots']['main']['*']
        return revision['*']

    def token(self, type_):
        result = self.api_request({'action': 'query',
                                   'meta': 'tokens',
                                   'type': type_})
        return result['query']['tokens'][type_ + 'token']

    def login(self, username, password):
        result = self.api_request({'action': 'login',
                                   'lgname': username,
                                   'lgpassword': password,
                                   'lgtoken': self.token('login')})
        if result['login'].get('result') != 'Success':
            raise APIError('login failed: %s' % result['login'].get('reason'))

    def edit(self, title, text, summary, section, bot):
        self.api_request({'action': 'edit',
                          'title': title,
                          'text': text,
                          'summary': summary,
                          'section': section,
                          'bot': bot,
                          'token': self.token('csrf')})


metawiki = Wiki(metawiki_base)
enwiki = Wiki(enwiki_base)
enwikt = Wiki(enwikt_base)
enquote = Wiki(enquote_base)

# Figure out the date
date = args.date
year = date.year
day = date.day
month = date.strftime('%B')
if DEBUG_MODE:
    print(month, day, year)

# Empty list
final_sections = []

def strip_html(original_text):
    soup = BeautifulSoup(original_text, 'html.parser')
    new_text = ''.join(soup.find_all(string=True))
    return new_text

def parse_wikitext(wiki, wikitext):
    params = {'action' : 'parse',
              'text'   : wikitext,
              'disablepp' : 'true'}
    response = wiki.api_request(params)
    parsed_wikitext = response['parse']['text']['*']
    return parsed_wikitext

def unescape(text):
    def fixup(m):
        text = m.group(0)
        if text[:2] == '&#':
            # character reference
            try:
                if text[:3] == '&#x':
                    return chr(int(text[3:-1], 16))
                else:
                    return chr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = chr(html.entities.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub(r'&#?\w+;', fixup, text)

def make_featured_article_section(month, day, year):
    page_title = '%s/%s %s, %s' % ("Wikipedia:Today's featured article",
                                   month,
                                   day,
                                   year)
    try:
        wikitext = enwiki.get_wikitext(page_title)
    except NoPage:
        return False
    parsed_wikitext = parse_wikitext(enwiki, wikitext)
    wrapper_div = '<div class="mw-content-ltr mw-parser-output" lang="en" dir="ltr">'
    if parsed_wikitext.startswith(wrapper_div):
        parsed_wikitext = parsed_wikitext.replace(wrapper_div, '')
    # Grab the first <p> tag and pray
    found = False
    for line in parsed_wikitext.split('\n'):
        if line.startswith('<p>') and not found:
            first_para = line
            found = True
    # The paragraph trails off into a "(Full article...)" parenthetical and the
    # prose is everything before it. Find that element by identity instead of
    # probing the raw HTML for '. (' and friends: the parser wraps the
    # parenthetical in <i>(<b><a ...>Full&#160;article...</a></b>)</i>, so those
    # literal probes never match and the fragment leaked into the emailed text.
    para_soup = BeautifulSoup(first_para, 'html.parser')
    read_more_link = None
    for a in para_soup.find_all('a'):
        if a.get_text().replace('\xa0', ' ').strip().startswith('Full article'):
            read_more_link = a
    if read_more_link is not None:
        read_more_href = read_more_link['href']
        featured_article_title = read_more_link['title']
        # Take the parentheses with it when they wrap the link (always <i> so
        # far), and keep the prose's own full stop: it used to be consumed by
        # the split at '. (' and re-appended by hand.
        parenthetical = read_more_link.find_parent('i') or read_more_link
        parenthetical.extract()
        p_text = para_soup.decode_contents()
    else:
        # Unrecognised markup: keep the old behaviour rather than guessing.
        p_text = first_para.rsplit('. (', 1)[0]+'.'
        if (first_para.find('. (') != -1 and
            first_para[:100].find('._(') != -1):
            more_html = first_para.rsplit('. (', 2)
            more_html = '. ('.join([more_html[1], more_html[2]])
        elif first_para.find('. (') != -1:
            more_html = first_para.rsplit('. (', 1)[1]
        elif first_para.find('." (') != -1:
            more_html = first_para.rsplit('." (', 1)[1]
        else:
            more_html = first_para
        more_soup = BeautifulSoup(more_html, 'html.parser')
        for a in more_soup.find_all('a'):
            read_more_href = a['href']
            featured_article_title = a['title']
    p_text = unescape(p_text)
    clean_p_text = strip_html(p_text)
    read_more = ('%s' + '<%s%s>') % ('Read more: ',
                                     enwiki_base,
                                     read_more_href.replace('(', '%28').replace(')', '%29'))
    featured_article_section = '\n'.join([wrap_text(clean_p_text),
                                          '',
                                          read_more,
                                          ''])
    final_sections.append(featured_article_section)
    return featured_article_title

def drop_pictured_marker(line):
    """Drop the "(pictured)" marker, and the space that held it apart.

    Hiding the marker behind string replacements only worked while it was
    followed by a space or by ", ": the parser also emits
    "... <i>(pictured)</i>." and there the marker leaked into the emailed text.
    """
    line_soup = BeautifulSoup(line, 'html.parser')
    marker = None
    for italic in line_soup.find_all('i'):
        if italic.get_text().replace('\xa0', ' ').strip() == '(pictured)':
            marker = italic
    if marker is None:
        # Unrecognised markup: keep the replacements this used to make.
        line = line.replace(' <i>(pictured)</i> ', ' ')
        return line.replace(' <i>(pictured)</i>, ', ', ')
    # The space before the marker goes with it, so that removing it cannot
    # leave "... Furness ." behind.
    previous = marker.previous_sibling
    if previous is not None and isinstance(previous, str):
        previous.replace_with(previous.rstrip())
    marker.extract()
    return line_soup.decode_contents()

def make_selected_anniversaries_section(month, day):
    page_title = 'Wikipedia:Selected anniversaries/%s %s' % (month, day)
    parsed_wikitext = parse_wikitext(enwiki, '{{'+page_title+'}}')
    anniversaries = []
    for line in parsed_wikitext.split('\n'):
        if line.startswith('<li') and line.find('\u2013') != -1:
            line = drop_pictured_marker(line)
            line = re.sub(r'<span class="nowrap">(.+?)</span>', r'\1', line)
            plaintext_lines = wrap_text(strip_html(unescape(line)))
            formatted_plaintext_lines = ':\n\n'.join(plaintext_lines.split(' \u2013 ', 1))
            line_soup = BeautifulSoup(line, 'html.parser')
            for b in line_soup.find_all('b'):
                if (len(b.contents) == 3 and
                    (b.contents[0] == b.contents[2] == '"')):
                    b.contents.pop()
                    b.contents.pop(0)
                if (len(b.contents) == 2 and
                    b.contents[1] == "'"):
                    b.contents.pop()
                for a in b.contents:
                    read_more = ('<%s%s>') % (enwiki_base,
                                              a['href'].replace('(', '%28').replace(')', '%29'))
            complete_item = formatted_plaintext_lines+'\n'+read_more+'\n'
            anniversaries.append(complete_item)
    header = '_______________________________\n'
    header += 'Today\'s selected anniversaries:\n'
    selected_anniversaries_section = '\n'.join([header,
                                                '\n'.join(anniversaries)])
    final_sections.append(selected_anniversaries_section)
    return

def definition_text(element):
    """The text of a definition element, without any sub-list hanging off it."""
    element_soup = BeautifulSoup(element.decode_contents(), 'html.parser')
    for ol in element_soup.find_all('ol'):
        ol.decompose()
    return unescape(strip_html(element_soup.decode_contents())).strip()

def format_definition(text, sub_definitions):
    """One definition, with the sub-senses that hang off it indented below it."""
    lines = wrap_text(text).split('\n')
    for sub in sub_definitions:
        lines += ['    ' + line for line in wrap_text(sub).split('\n')]
    return '\n'.join(lines)

def make_wiktionary_section(month, day, year):
    page_title = 'Wiktionary:Word of the day/%s/%s %s' % (year, month, day)
    page_url = enwikt_base + '/wiki/' + page_title.replace(' ', '_')
    if DEBUG_MODE:
        print(page_url)
    parsed_wikitext = parse_wikitext(enwikt, '{{'+page_title+'}}')
    soup = BeautifulSoup(parsed_wikitext, 'html.parser')
    word = soup.find('span', id='WOTD-rss-title').string

    # The definitions hang off one container; every other list on the page is
    # navigation chrome ("About Word of the Day" and friends) that a flat walk
    # over <li> used to sweep up and number as if it were a sense. They sit in
    # one <ol> per part of speech, and a sub-sense is an <li> inside a
    # definition's own <li>, so the top-level ones are those with no <li> above
    # them.
    container = soup.find('div', id='WOTD-rss-description')
    if container is not None:
        definition_items = [li for li in container.find_all('li')
                            if li.find_parent('li') is None]
    else:
        # Unrecognised markup: keep the old flat walk rather than print nothing.
        definition_items = soup.find_all('li')
    definitions_stripped = []
    for li in definition_items:
        # Sub-senses belong to the definition they hang from: carry them along
        # rather than decomposing their list and promoting each one to a sense.
        definitions_stripped.append((definition_text(li),
                                     [definition_text(sub) for sub in li.find_all('li')]))
    definitions = []
    if len(definitions_stripped) > 1:
        for i, (text, sub_definitions) in enumerate(definitions_stripped):
            definitions.append(format_definition(str(i + 1) + '. ' + text, sub_definitions))
    elif len(definitions_stripped) == 1:
        text, sub_definitions = definitions_stripped[0]
        definitions = [format_definition(text, sub_definitions)]
    if not definitions:
        message = 'no definitions found on %s' % page_url
        if args.force:
            print(message, file=sys.stderr)
        else:
            raise ValueError(message)
        return

    header = '_____________________________\n'
    header += 'Wiktionary\'s word of the day:\n'
    read_more = '<'+enwikt_base+'/wiki/'+urllib.parse.quote(word.replace(' ', '_'))+'>'
    wiktionary_section = '\n'.join([header,
                                    word+':',
                                    '\n'.join(definitions),
                                    read_more,
                                    ''])
    if DEBUG_MODE:
        print(repr(wiktionary_section))
    final_sections.append(wiktionary_section)
    return

def make_wikiquote_section(month, day, year):
    page_title = 'Wikiquote:Quote of the day/%s %s, %s' % (month, day, year)
    parsed_wikitext = parse_wikitext(enquote, '{{'+page_title+'}}')
    lines = []
    author = None
    read_more = None
    for line in parsed_wikitext.split('\n'):
        if line.find('\u2014') != -1:
            author_soup = BeautifulSoup(line, 'html.parser')
            for a in author_soup.find_all('a'):
                if not a.string:
                    continue
                read_more = ('<%s%s>') % (enquote_base,
                                          a['href'].replace('(', '%28').replace(')', '%29'))
                author = '  --'+a.string
        elif line != 'in<br />':
            lines.append(unescape(line))
    if author is None or read_more is None:
        page_url = enquote_base + '/wiki/' + page_title.replace(' ', '_')
        message = 'no quote/author line found on %s' % page_url
        if args.force:
            print(message, file=sys.stderr)
            return
        else:
            raise ValueError(message)
    authorless_lines = '\n'.join(lines)
    quote = strip_html(authorless_lines)
    quote = quote.strip()
    quote = quote.replace('\u201c\n\n', '').replace('\n\n\u201d', '')
    header = '___________________________\n'
    header += 'Wikiquote quote of the day:\n'
    wikiquote_section = '\n'.join([header,
                                   wrap_text(quote),
                                   author,
                                   read_more])
    final_sections.append(wikiquote_section)
    return

def wrap_text(text):
    wrapped_lines = textwrap.wrap(text, width=72)
    return '\n'.join(wrapped_lines)

def build_email(email_to, email_from, email_subject, email_body):
    msg = MIMENonMultipart('text', 'plain')
    msg['Content-Transfer-Encoding'] = '8bit'
    msg.set_payload(email_body, 'utf-8')
    msg['From'] = email_from
    msg['Subject'] = Header(email_subject, 'utf-8')
    msg['To'] = email_to
    return msg

def send_email(email_to, email_from, email_subject, email_body):
    server = smtplib.SMTP(config.smtp_host, config.smtp_port)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(email_from, config.email_password)
    for addr in email_to:
        msg = build_email(addr, email_from, email_subject, email_body)
        server.sendmail(email_from, addr, msg.as_bytes(), '8bitmime')
    server.quit()

send = False
try:
    # Do some shit
    featured_article_title = make_featured_article_section(month, day, year)
    subject = '%s %d: %s' % (month, day, featured_article_title)
    make_selected_anniversaries_section(month, day)
    make_wiktionary_section(month, day, year)
    make_wikiquote_section(month, day, year)

    final_output = '\n'.join(final_sections)
    send = True

except:  # Unnamed!
    # Inform the wiki of an issue!
    date = '%s %s, %s' % (month, day, year)
    tb = traceback.format_exc().rstrip().replace(config.wiki_password, 'XXX')
    if DEBUG_MODE:
        print(tb)
        sys.exit(1)
    else:
        text = '\n'.join(("Just thought you'd like to know:",
                          "<pre>",
                          tb,
                          "</pre>",
                          "Love, --~~~~"))
        metawiki.login(config.wiki_username, config.wiki_password)
        metawiki.edit(config.notification_page,
                      text=text,
                      summary='daily-article-l delivery failed (%s)' % date,
                      section='new',
                      bot=1)

if args.output:
    # The artifact is addressed to the sender: it is not being mailed anywhere.
    with open(args.output, 'wb') as handle:
        handle.write(build_email(config.from_address, config.from_address,
                                 subject, final_output).as_bytes())
elif DEBUG_MODE:
    print(subject + '\n')
    print('\n'.join(final_sections))
elif send:
    send_email(config.to_addresses,
               config.from_address,
               subject,
               final_output)
