import base64
import email
import os
import re
import sys
from email.message import EmailMessage, Message
from typing import Tuple, Dict, Match
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

"""
loosly based on: https://github.com/ssato/python-smhtml 
"""

# Don't use pip._vendor.chardet, use chardet directly
import chardet

# find url of CSS property value: url(https://…/image.png)
# except with preceding: @import
url_only_pattern_text = r'url\(([^\)]+)\)'
url_only_pattern = re.compile(url_only_pattern_text)
css_url_pattern = re.compile(url_only_pattern_text + r'([ ]*[^;]*);?')

bad_urls = [
    'bat.r.msn.com',
    'bat.bing.com',
    'adroll.com',
    'facebook.com',
    'googleads.g.doubleclick.net',
    'secure.adnxs.com'
]

parts = {}


def detect_charset(bmsg, default="ascii"):
    r"""
    :param bmsg: A byte data to detect charset
    :return: A string represents charset such as 'utf-8', 'iso-2022-jp'
    >>> detect_charset(b"a")
    'ascii'
    >>> detect_charset(b"")
    'ascii'
    >>> detect_charset(u"あ".encode("utf-8"))
    'utf-8'
    """
    if not bmsg:
        return default

    return chardet.detect(bmsg)["encoding"]


def decode_message(message: Message):
    """
    Decode a part of MIME multi-part data.
    :param message: :class:`email.mime.base.MIMEBase` object
    :return: A dict contains various info of given MIME `part` data
    """
    bdata = message.get_payload(decode=True)
    ctype = message.get_content_type()
    mtype = message.get_content_maintype()

    if mtype == "text":
        charset = detect_charset(bdata)
        if charset:
            data = bdata.decode(charset, "ignore")
        else:
            data = bdata
    else:
        charset = None
        data = bdata

    location = message.get_all("Content-Location")
    return dict(
        type=ctype,
        encoding=charset,
        data=data,
        payload=bdata,
        location=location[0] if location else None
    )


def parse_itr(msg: EmailMessage):
    """
    An iterator to yield each info from given MIME multi-message data.
    :param msg: :class:`email.message.Message` object
    :return: A generator yields info of each message in `mdata`
    """
    for idx, message in enumerate(msg.walk()):
        if message.get_content_maintype() == "multipart":
            continue

        info = decode_message(message)
        info["index"] = idx

        url_parts = list(urlparse(info["location"]))
        info["location.path"] = url_parts[2]

        yield info


def load_itr(file_path):
    """
    An iterator to yield each info from given MIME multi-message data as a file
    after some checks.
    :param file_path: :class:`pathlib.Path` object or a string represents path
    :return: A generator yields each message parsed from `filepath` opened
    :raises: ValueError
    """

    # file encoding can be utf-8 or iso-8859-1
    with open(file_path, 'rb') as f:
        # Join binary lines for specified number of lines
        rawdata = b''.join([f.readline() for _ in range(20)])

    encoding = chardet.detect(rawdata)['encoding']
    # ascii shall default to utf-8, otherwise can lead to decoding errors
    encoding = 'utf-8' if encoding == 'ascii' else encoding

    try:
        with open(file_path, mode='r', encoding=encoding) as fobj:
            msg = email.message_from_file(fobj)
    except UnicodeDecodeError:
        # fallback if still not working correctly
        with open(file_path, mode='r', encoding='iso-8859-1') as fobj:
            msg = email.message_from_file(fobj)

    if not msg.is_multipart():
        raise ValueError("Multi-message MIME data was not found in "
                         "'%s'" % file_path)

    # for info in parse_itr(msg):
    #     yield info
    yield from parse_itr(msg)


def extract(filepath, output, usebasename=False, outputfilenamer=None):
    """
    Load and extract each message of MIME multi-message data as files from given data
    as a file.
    :param filepath: :class:`pathlib.Path` object represents input
    :param output: :class:`pathlib.Path` object represents output dir
    :param usebasename: Use the basename, not full path, when writing files
    :param outputfilenamer: Callback fn takes `inf` and returns a filename
    For example, it could return a filename based on `inf['location']`
    :raises: ValueError
    """
    if output == "-":
        raise ValueError("Output dir must be given to extract")

    if os.path.exists(output) and os.path.isfile(output):
        raise OSError("Output '%s' already exists as a file!" % output)

    os.makedirs(output)
    for inf in load_itr(filepath):
        filename = inf["filename"]

        if usebasename:
            filename = os.path.split(filename)[-1]

        if outputfilenamer:
            filename = outputfilenamer(inf)

        outpath = os.path.join(output, filename)
        outdir = os.path.dirname(outpath)

        if not os.path.exists(outdir):
            os.makedirs(outdir)

        with open(outpath, "wb") as out:
            out.write(inf["payload"])


def tag_img_and_src(tag):
    return tag.name == 'img' and tag.has_attr('src')


def tag_has_style_attribute(tag):
    return tag.has_attr('style')


def is_url_bad(url: str) -> bool:
    for bad_url in bad_urls:
        if bad_url in url:
            return True


def replace_urls(css: str) -> str:
    return re.sub(css_url_pattern, url_replacer, css)


def url_replacer(match: Match):
    url = match[1].strip("\"'")
    suffix = match[2]

    if url in parts:
        part = parts[url]
        part['used'] = True
        if isinstance(part['data'], bytes):
            payload = part['data']
        else:
            payload = part['data'].encode('utf-8')

        return 'url("data:{};base64,{}"){};'.format(part['type'], encode_bytes_to_hex(payload), suffix)
    else:
        return match[0]


def san_urls(data_raw: str):
    """ Convert relative to absolute references in a CSS sheet. """
    return re.sub(url_only_pattern, san_url_fn, data_raw)


def san_url_fn(match: Match):
    url = match[1].strip("\"'")

    url_is_relative = url != '' \
                      and not url.startswith('data:') \
                      and not url_is_absolute(url)

    if url_is_relative:
        new_url = urljoin(base_location, url)
        new_value = f'url("{new_url}")'
        return new_value

    return match[0]


def embed_files_css(html_body: BeautifulSoup, base_url: str):
    # <link rel="stylesheet" href="_static/classic.css" type="text/css" />
    for tag in html_body.findAll('link', rel='stylesheet'):
        url = tag.get('href')
        if not url:
            continue

        url_is_relative = url != '' and not url_is_absolute(url)

        if url_is_relative:
            url = urljoin(base_url, url)

        # add scheme to url, if missing
        base_scheme = urlparse(base_url).scheme
        url = urlparse(url, base_scheme).geturl()

        if url in parts:
            part = parts[url]
            # if part['location.path'] == tag_path:
            global base_location
            base_location = part['location']
            data = san_urls(part['data'])
            base_location = None
            data2 = replace_urls(data)

            style_tag = html_body.new_tag('style')
            style_tag.string = data2
            tag.replaceWith(style_tag)

            part['used'] = True
        # else:
        #     eprint('other css: ' + tag['href'])


def embed_files_img_with_src(html_body: BeautifulSoup, base_url: str):
    if base_url.startswith('https'):
        scheme = 'https'
    else:
        scheme = 'http'

    for tag in html_body.findAll(tag_img_and_src):
        tag_url = tag['src']

        # transform to absolute path
        tag_url_is_relative = tag_url != '' and not url_is_absolute(tag_url)
        if tag_url_is_relative:
            tag_url = urljoin(base_url, tag_url)

        # add scheme to url, if missing
        base_scheme = urlparse(base_url).scheme
        tag_url = urlparse(tag_url, base_scheme).geturl()

        if tag_url in parts:
            part = parts[tag_url]
            # if part['location.path'] == tag_path:
            part_raw_data = part['data']

            if type(part_raw_data) is str:
                continue

            tag['src'] = 'data:{};base64,{}'.format(part['type'], encode_bytes_to_hex(part_raw_data))

            part['used'] = True
        else:
            # remove tracking images
            if is_url_bad(tag['src']):
                tag.decompose()
        # else:
        #     eprint('other img: ' + tag['src'])


def encode_bytes_to_hex(ibytes: bytes):
    image_bytes = base64.b64encode(ibytes)
    encoded_image = image_bytes.decode('ascii')
    return encoded_image


def remove_script(html_body: BeautifulSoup):
    # <script src="script.js"></script>
    for tag in html_body.findAll('script'):
        tag.decompose()


def remove_link_dns_prefetch(html_body: BeautifulSoup):
    # <link href="//s0.wp.com" rel="dns-prefetch">
    for tag in html_body.findAll('link', rel='dns-prefetch'):
        tag.decompose()


def url_is_absolute(url):
    return bool(urlparse(url).netloc)


def embed_images_in_style_tags(html_body: BeautifulSoup, base_url: str):
    """
    Replace URL references in the CSS files:
    background:url(https://…/abc.png
    =>
    url(data:image/gif;base64,R0lGODl…)

    :param html_body:
    :param base_url:
    :return:
    """
    for tag in html_body.findAll('style'):
        global base_location
        base_location = base_url
        content = tag.string

        if content != None:
            data = san_urls(tag.string)
            base_location = None
            tag.string = replace_urls(data)


def embed_images_in_style_attributes(html_body: BeautifulSoup, base_url: str):
    """
    Replace URL references in the CSS files:
    background:url(https://…/abc.png
    =>
    url(data:image/gif;base64,R0lGODl…)

    :param html_body:
    :param base_url:
    :return:
    """
    for tag in html_body.findAll(tag_has_style_attribute):
        global base_location
        base_location = base_url
        data = san_urls(tag['style'])
        base_location = None
        tag['style'] = replace_urls(data)


def convert(filepath) -> str:
    """
    Load and extract each message of MIME multi-message data as files from given data
    as a file.
    :param filepath: :class:`pathlib.Path` object represents input
    For example, it could return a filename based on `message['location']`
    :raises: ValueError
    """

    parts_iterator = load_itr(filepath)
    html = next(parts_iterator)
    payload_raw = html['payload']

    try:
        payload = payload_raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            payload = payload_raw.decode('cp1252')
        except ValueError:
            try:
                payload = payload_raw.decode('iso-8859-1')
            except ValueError as ve:
                print("failed to decode file content (utf-8, cp1252): {0}".format(ve))
                sys.exit(1)

    # fix: ugly body definition on digitalocean: <body … ,="" …>
    pattern = '<body (.*) ,=""'
    replacement = r'<body \1'
    payload = re.sub(pattern, replacement, payload, 1)

    if html["type"] == 'text/html':
        html_body = BeautifulSoup(payload, features="html.parser")
    else:
        raise ValueError('no leading html message found')

    global parts
    parts = dict([(p['location'], p) for p in parts_iterator])

    remove_script(html_body)
    remove_link_dns_prefetch(html_body)
    embed_images_in_style_tags(html_body, html['location'])
    embed_images_in_style_attributes(html_body, html['location'])
    embed_files_css(html_body, html['location'])
    embed_files_img_with_src(html_body, html['location'])

    for key, part in parts.items():
        if 'used' not in part:
            eprint(part['location'])

    return str(html_body)


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def cli_entrypoint():
    input_file = sys.argv[1]
    converted_html = convert(input_file)

    output_file = input_file + '.html'
    with open(output_file, 'w') as text_file:
        text_file.write(converted_html)

if __name__ == '__main__':
    cli_entrypoint()
