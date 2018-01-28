#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import os
import platform
import shutil
import tarfile
import threading
import time
import traceback

import lazylibrarian
try:
    import zipfile
except ImportError:
    import lib.zipfile as zipfile
from lazylibrarian import database, logger, utorrent, transmission, qbittorrent, \
    deluge, rtorrent, synology, sabnzbd, nzbget
from lazylibrarian.bookwork import audioRename, seriesInfo
from lazylibrarian.cache import cache_img
from lazylibrarian.calibre import calibredb
from lazylibrarian.common import scheduleJob, book_file, opf_file, setperm, bts_file, jpg_file
from lazylibrarian.formatter import unaccented_str, unaccented, plural, now, today, is_valid_booktype, \
    replace_all, getList, surnameFirst, makeUnicode, makeBytestr
from lazylibrarian.gr import GoodReads
from lazylibrarian.importer import addAuthorToDB, addAuthorNameToDB, update_totals
from lazylibrarian.librarysync import get_book_info, find_book_in_db, LibraryScan
from lazylibrarian.magazinescan import create_id, create_cover
from lazylibrarian.notifiers import notify_download, custom_notify_download
from lib.deluge_client import DelugeRPCClient
from lib.fuzzywuzzy import fuzz
from lib.six import PY2

# Need to remove characters we don't want in the filename BEFORE adding to EBOOK_DIR
# as windows drive identifiers have colon, eg c:  but no colons allowed elsewhere?
__dic__ = {'<': '', '>': '', '...': '', ' & ': ' ', ' = ': ' ', '?': '', '$': 's',
           ' + ': ' ', '"': '', ',': '', '*': '', ':': '', ';': '', '\'': '', '//': '/', '\\\\': '\\'}


def update_downloads(provider):
    myDB = database.DBConnection()
    entry = myDB.match('SELECT Count FROM downloads where Provider=?', (provider,))
    if entry:
        counter = int(entry['Count'])
        myDB.action('UPDATE downloads SET Count=? WHERE Provider=?', (counter + 1, provider))
    else:
        myDB.action('INSERT into downloads (Count, Provider) VALUES  (?, ?)', (1, provider))


def processAlternate(source_dir=None):
    # import a book from an alternate directory
    # noinspection PyBroadException
    try:
        if not source_dir or not os.path.isdir(source_dir):
            logger.warn("Alternate Directory not configured")
            return False
        if source_dir == lazylibrarian.DIRECTORY('eBook'):
            logger.warn('Alternate directory must not be the same as Destination')
            return False

        logger.debug('Processing alternate directory %s' % source_dir)
        # first, recursively process any books in subdirectories
        flist = os.listdir(makeBytestr(source_dir))
        flist = [makeUnicode(item) for item in flist]
        for fname in flist:
            subdir = os.path.join(source_dir, fname)
            if os.path.isdir(subdir):
                processAlternate(subdir)
        # only import one book from each alternate (sub)directory, this is because
        # the importer may delete the directory after importing a book,
        # depending on lazylibrarian.CONFIG['DESTINATION_COPY'] setting
        # also if multiple books in a folder and only a "metadata.opf"
        # which book is it for?
        new_book = book_file(source_dir, booktype='ebook')
        if new_book:
            metadata = {}
            # see if there is a metadata file in this folder with the info we need
            # try book_name.opf first, or fall back to any filename.opf
            metafile = os.path.splitext(new_book)[0] + '.opf'
            if not os.path.isfile(metafile):
                metafile = opf_file(source_dir)
            if metafile and os.path.isfile(metafile):
                try:
                    metadata = get_book_info(metafile)
                except Exception as e:
                    logger.debug('Failed to read metadata from %s, %s %s' % (metafile, type(e).__name__, str(e)))
            else:
                logger.debug('No metadata file found for %s' % new_book)
            if 'title' not in metadata or 'creator' not in metadata:
                # if not got both, try to get metadata from the book file
                try:
                    metadata = get_book_info(new_book)
                except Exception as e:
                    logger.debug('No metadata found in %s, %s %s' % (new_book, type(e).__name__, str(e)))
            if 'title' in metadata and 'creator' in metadata:
                authorname = metadata['creator']
                bookname = metadata['title']
                myDB = database.DBConnection()
                authorid = ''
                authmatch = myDB.match('SELECT * FROM authors where AuthorName=?', (authorname,))

                if not authmatch:
                    # try goodreads preferred authorname
                    logger.debug("Checking GoodReads for [%s]" % authorname)
                    GR = GoodReads(authorname)
                    try:
                        author_gr = GR.find_author_id()
                    except Exception as e:
                        author_gr = {}
                        logger.debug("No author id for [%s] %s" % (authorname, type(e).__name__))
                    if author_gr:
                        grauthorname = author_gr['authorname']
                        authorid = author_gr['authorid']
                        logger.debug("GoodReads reports [%s] for [%s]" % (grauthorname, authorname))
                        authorname = grauthorname
                        authmatch = myDB.match('SELECT * FROM authors where AuthorID=?', (authorid,))

                if authmatch:
                    logger.debug("ALT: Author %s found in database" % authorname)
                else:
                    logger.debug("ALT: Author %s not found, adding to database" % authorname)
                    if authorid:
                        addAuthorToDB(authorid=authorid)
                    else:
                        addAuthorNameToDB(author=authorname)

                bookid = find_book_in_db(authorname, bookname)
                if bookid:
                    return import_book(source_dir, bookid)
                else:
                    logger.warn("Book %s by %s not found in database" % (bookname, authorname))
            else:
                logger.warn('Book %s has no metadata, unable to import' % new_book)
        else:
            # could check if an archive in this directory?
            logger.warn("No book file found in %s" % source_dir)
        return False
    except Exception:
        logger.error('Unhandled exception in processAlternate: %s' % traceback.format_exc())


def move_into_subdir(sourcedir, targetdir, fname, move='move'):
    # move the book and any related files too, other book formats, or opf, jpg with same title
    # (files begin with fname) from sourcedir to new targetdir
    # can't move metadata.opf or cover.jpg or similar as can't be sure they are ours
    list_dir = os.listdir(makeBytestr(sourcedir))
    list_dir = [makeUnicode(item) for item in list_dir]
    for ourfile in list_dir:
        if int(lazylibrarian.LOGLEVEL) > 2:
            logger.debug("Checking %s for %s" % (ourfile, fname))
        if ourfile.startswith(fname) or is_valid_booktype(ourfile, booktype="audiobook"):
            if is_valid_booktype(ourfile, booktype="book") \
                    or is_valid_booktype(ourfile, booktype="audiobook") \
                    or is_valid_booktype(ourfile, booktype="mag") \
                    or os.path.splitext(ourfile)[1].lower() in ['.opf', '.jpg']:
                try:
                    if lazylibrarian.CONFIG['DESTINATION_COPY'] or move == 'copy':
                        shutil.copyfile(os.path.join(sourcedir, ourfile), os.path.join(targetdir, ourfile))
                        setperm(os.path.join(targetdir, ourfile))
                    else:
                        shutil.move(os.path.join(sourcedir, ourfile), os.path.join(targetdir, ourfile))
                        setperm(os.path.join(targetdir, ourfile))
                except Exception as why:
                    logger.debug("Failed to copy/move file %s to [%s], %s %s" %
                                 (ourfile, targetdir, type(why).__name__, str(why)))


def unpack_archive(pp_path, download_dir, title):
    """ See if pp_path is an archive containing a book
        returns new directory in download_dir with book in it, or empty string """
    # noinspection PyBroadException
    try:
        from lib.unrar import rarfile
        gotrar = True
    except Exception:
        gotrar = False
        rarfile = None

    targetdir = ''
    if not os.path.isfile(pp_path):  # regular files only
        targetdir = ''
    elif zipfile.is_zipfile(pp_path):
        if int(lazylibrarian.LOGLEVEL) > 2:
            logger.debug('%s is a zip file' % pp_path)
        z = zipfile.ZipFile(pp_path)
        namelist = z.namelist()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    try:
                        os.makedirs(targetdir)
                        setperm(targetdir)
                    except OSError as why:
                        if not os.path.isdir(targetdir):
                            logger.debug('Failed to create dir [%s], %s' % (targetdir, why.strerror))
                            return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.read(item))
            else:
                logger.debug('Skipping zipped file %s' % item)

    elif tarfile.is_tarfile(pp_path):
        if int(lazylibrarian.LOGLEVEL) > 2:
            logger.debug('%s is a tar file' % pp_path)
        z = tarfile.TarFile(pp_path)
        namelist = z.getnames()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    try:
                        os.makedirs(targetdir)
                        setperm(targetdir)
                    except OSError as why:
                        if not os.path.isdir(targetdir):
                            logger.debug('Failed to create dir [%s], %s' % (targetdir, why.strerror))
                            return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.extractfile(item).read())
            else:
                logger.debug('Skipping tarred file %s' % item)

    elif gotrar and rarfile.is_rarfile(pp_path):
        if int(lazylibrarian.LOGLEVEL) > 2:
            logger.debug('%s is a rar file' % pp_path)
        z = rarfile.RarFile(pp_path)
        namelist = z.namelist()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    try:
                        os.makedirs(targetdir)
                        setperm(targetdir)
                    except OSError as why:
                        if not os.path.isdir(targetdir):
                            logger.debug('Failed to create dir [%s], %s' % (targetdir, why.strerror))
                            return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.read(item))
            else:
                logger.debug('Skipping rarred file %s' % item)
    else:
        logger.debug('[%s] Not a recognised archive' % pp_path)
    return targetdir


def cron_processDir():
    if 'POSTPROCESS' not in [n.name for n in [t for t in threading.enumerate()]]:
        processDir()


def processDir(reset=False):
    # noinspection PyBroadException,PyStatementEffect
    try:
        threadname = threading.currentThread().name
        if "Thread-" in threadname:
            threading.currentThread().name = "POSTPROCESS"
        ppcount = 0
        myDB = database.DBConnection()
        skipped_extensions = ['.fail', '.part', '.bts', '.!ut', '.torrent', '.magnet', '.nzb', '.unpack']

        templist = getList(lazylibrarian.CONFIG['DOWNLOAD_DIR'], ',')
        if len(templist) and lazylibrarian.DIRECTORY("Download") != templist[0]:
            templist.insert(0, lazylibrarian.DIRECTORY("Download"))
        dirlist = []
        for item in templist:
            if os.path.isdir(item):
                dirlist.append(item)
        for download_dir in dirlist:
            try:
                downloads = os.listdir(makeBytestr(download_dir))
                downloads = [makeUnicode(item) for item in downloads]
            except OSError as why:
                logger.error('Could not access directory [%s] %s' % (download_dir, why.strerror))
                threading.currentThread().name = "WEBSERVER"
                return

            snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
            logger.debug('Found %s file%s marked "Snatched"' % (len(snatched), plural(len(snatched))))
            logger.debug('Checking %s file%s in %s' % (len(downloads), plural(len(downloads)), download_dir))

            if len(snatched) > 0 and len(downloads) > 0:
                for book in snatched:
                    # if torrent, see if we can get current status from the downloader as the name
                    # may have been changed once magnet resolved, or download started or completed
                    # depending on torrent downloader. Usenet doesn't change the name. We like usenet.
                    torrentname = ''
                    try:
                        logger.debug("%s was sent to %s" % (book['NZBtitle'], book['Source']))
                        if book['Source'] == 'TRANSMISSION':
                            torrentname = transmission.getTorrentFolder(book['DownloadID'])
                        elif book['Source'] == 'UTORRENT':
                            torrentname = utorrent.nameTorrent(book['DownloadID'])
                        elif book['Source'] == 'RTORRENT':
                            torrentname = rtorrent.getName(book['DownloadID'])
                        elif book['Source'] == 'QBITTORRENT':
                            torrentname = qbittorrent.getName(book['DownloadID'])
                        elif book['Source'] == 'SYNOLOGY_TOR':
                            torrentname = synology.getName(book['DownloadID'])
                        elif book['Source'] == 'DELUGEWEBUI':
                            torrentname = deluge.getTorrentFolder(book['DownloadID'])
                        elif book['Source'] == 'DELUGERPC':
                            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'],
                                                     lazylibrarian.CONFIG['DELUGE_URL_BASE'],
                                                     int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                                     lazylibrarian.CONFIG['DELUGE_USER'],
                                                     lazylibrarian.CONFIG['DELUGE_PASS'])
                            try:
                                client.connect()
                                result = client.call('core.get_torrent_status', book['DownloadID'], {})
                                #    for item in result:
                                #        logger.debug ('Deluge RPC result %s: %s' % (item, result[item]))
                                if 'name' in result:
                                    torrentname = unaccented_str(result['name'])
                            except Exception as e:
                                logger.debug('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))
                    except Exception as e:
                        logger.debug("Failed to get updated torrent name from %s for %s: %s %s" %
                                     (book['Source'], book['DownloadID'], type(e).__name__, str(e)))

                    matchtitle = unaccented_str(book['NZBtitle'])
                    if torrentname and torrentname != matchtitle:
                        logger.debug("%s Changing [%s] to [%s]" % (book['Source'], matchtitle, torrentname))
                        # should we check against reject word list again as the name has changed?
                        myDB.action('UPDATE wanted SET NZBtitle=? WHERE NZBurl=?', (torrentname, book['NZBurl']))
                        matchtitle = torrentname

                    # here we could also check percentage downloaded or eta or status, or download directory?
                    # If downloader says it hasn't completed, no need to look for it.

                    matches = []

                    book_type = book['AuxInfo']
                    if book_type != 'AudioBook' and book_type != 'eBook':
                        if book_type is None or book_type == '':
                            book_type = 'eBook'
                        else:
                            book_type = 'Magazine'

                    logger.debug('Looking for %s %s in %s' % (book_type, matchtitle, download_dir))
                    for fname in downloads:
                        # skip if failed before or incomplete torrents, or incomplete btsync etc
                        if int(lazylibrarian.LOGLEVEL) > 2:
                            logger.debug("Checking extn on %s" % fname)
                        extn = os.path.splitext(fname)[1]
                        if not extn or extn not in skipped_extensions:
                            # This is to get round differences in torrent filenames.
                            # Usenet is ok, but Torrents aren't always returned with the name we searched for
                            # We ask the torrent downloader for the torrent name, but don't always get an answer
                            # so we try to do a "best match" on the name, there might be a better way...

                            matchname = fname
                            # torrents might have words_separated_by_underscores
                            matchname = matchname.split(' LL.(')[0].replace('_', ' ')
                            matchtitle = matchtitle.split(' LL.(')[0].replace('_', ' ')
                            match = fuzz.token_set_ratio(matchtitle, matchname)
                            if int(lazylibrarian.LOGLEVEL) > 2:
                                logger.debug("%s%% match %s : %s" % (match, matchtitle, matchname))
                            if match >= lazylibrarian.CONFIG['DLOAD_RATIO']:
                                pp_path = os.path.join(download_dir, fname)

                                if int(lazylibrarian.LOGLEVEL) > 2:
                                    logger.debug("processDir %s %s" % (type(pp_path), repr(pp_path)))

                                if os.path.isfile(pp_path):
                                    # Check for single file downloads first. Book/mag file in download root.
                                    # move the file into it's own subdirectory so we don't move/delete
                                    # things that aren't ours
                                    # note that epub are zipfiles so check booktype first
                                    #
                                    if is_valid_booktype(fname, booktype="book") \
                                            or is_valid_booktype(fname, booktype="audiobook") \
                                            or is_valid_booktype(fname, booktype="mag"):
                                        if int(lazylibrarian.LOGLEVEL) > 2:
                                            logger.debug('file [%s] is a valid book/mag' % fname)
                                        if bts_file(download_dir):
                                            logger.debug("Skipping %s, found a .bts file" % download_dir)
                                        else:
                                            aname = os.path.splitext(fname)[0]
                                            while aname[-1] in '. ':
                                                aname = aname[:-1]
                                            targetdir = os.path.join(download_dir, aname)
                                            if not os.path.isdir(targetdir):
                                                try:
                                                    os.makedirs(targetdir)
                                                    setperm(targetdir)
                                                except OSError as why:
                                                    if not os.path.isdir(targetdir):
                                                        logger.debug('Failed to create directory [%s], %s' %
                                                                     (targetdir, why.strerror))
                                            if os.path.isdir(targetdir):
                                                if book['NZBmode'] in ['torrent', 'magnet', 'torznab'] and \
                                                        lazylibrarian.CONFIG['KEEP_SEEDING']:
                                                    move_into_subdir(download_dir, targetdir, fname, move='copy')
                                                else:
                                                    move_into_subdir(download_dir, targetdir, fname)
                                                pp_path = targetdir
                                    else:
                                        # Is file an archive, if so look inside and extract to new dir
                                        res = unpack_archive(pp_path, download_dir, matchtitle)
                                        if res:
                                            pp_path = res
                                        else:
                                            logger.debug('Skipping unhandled file %s' % fname)

                                elif os.path.isdir(pp_path):
                                    logger.debug('Found folder (%s%%) [%s] for %s %s' %
                                                 (match, pp_path, book_type, matchtitle))

                                    for f in os.listdir(makeBytestr(pp_path)):
                                        f = makeUnicode(f)
                                        if not is_valid_booktype(f, 'book') \
                                                and not is_valid_booktype(f, 'audiobook') \
                                                and not is_valid_booktype(f, 'mag'):
                                            # Is file an archive, if so look inside and extract to new dir
                                            res = unpack_archive(os.path.join(pp_path, f), pp_path, matchtitle)
                                            if res:
                                                pp_path = res
                                                break

                                    skipped = False
                                    if book_type == 'eBook' and not book_file(pp_path, 'ebook'):
                                        logger.debug("Skipping %s, no ebook found" % pp_path)
                                        skipped = True
                                    elif book_type == 'AudioBook' and not book_file(pp_path, 'audiobook'):
                                        logger.debug("Skipping %s, no audiobook found" % pp_path)
                                        skipped = True
                                    elif book_type == 'Magazine' and not book_file(pp_path, 'mag'):
                                        logger.debug("Skipping %s, no magazine found" % pp_path)
                                        skipped = True
                                    if not os.listdir(makeBytestr(pp_path)):
                                        logger.debug("Skipping %s, folder is empty" % pp_path)
                                        skipped = True
                                    elif bts_file(pp_path):
                                        logger.debug("Skipping %s, found a .bts file" % pp_path)
                                        skipped = True
                                    if not skipped:
                                        matches.append([match, pp_path, book])
                                else:
                                    logger.debug('%s is not a file or a directory?' % pp_path)
                            else:
                                pp_path = os.path.join(download_dir, fname)
                                matches.append([match, pp_path, book])  # so we can report closest match
                        else:
                            logger.debug('Skipping %s' % fname)

                    match = 0
                    if matches:
                        highest = max(matches, key=lambda x: x[0])
                        match = highest[0]
                        pp_path = highest[1]
                        book = highest[2]
                    if match and match >= lazylibrarian.CONFIG['DLOAD_RATIO']:
                        mostrecentissue = ''
                        logger.debug('Found match (%s%%): %s for %s %s' % (match, pp_path, book_type, book['NZBtitle']))

                        cmd = 'SELECT AuthorName,BookName from books,authors WHERE BookID=?'
                        cmd += ' and books.AuthorID = authors.AuthorID'
                        data = myDB.match(cmd, (book['BookID'],))
                        if data:  # it's ebook/audiobook
                            logger.debug('Processing %s %s' % (book_type, book['BookID']))
                            authorname = data['AuthorName']
                            authorname = ' '.join(authorname.split())  # ensure no extra whitespace
                            bookname = data['BookName']
                            if 'windows' in platform.system().lower() and '/' in \
                                    lazylibrarian.CONFIG['EBOOK_DEST_FOLDER']:
                                logger.warn('Please check your EBOOK_DEST_FOLDER setting')
                                lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'] = lazylibrarian.CONFIG[
                                    'EBOOK_DEST_FOLDER'].replace('/', '\\')
                            # Default destination path, should be allowed change per config file.
                            dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
                                '$Author', authorname).replace(
                                '$Title', bookname).replace(
                                '$Series', seriesInfo(book['BookID'])).replace(
                                '$SerName', seriesInfo(book['BookID'], 'Name')).replace(
                                '$SerNum', seriesInfo(book['BookID'], 'Num')).replace(
                                '$$', ' ')
                            dest_path = ' '.join(dest_path.split()).strip()
                            dest_path = replace_all(dest_path, __dic__)
                            dest_dir = lazylibrarian.DIRECTORY('eBook')
                            if book_type == 'AudioBook' and lazylibrarian.DIRECTORY('Audio'):
                                dest_dir = lazylibrarian.DIRECTORY('Audio')
                            dest_path = os.path.join(dest_dir, dest_path)

                            global_name = lazylibrarian.CONFIG['EBOOK_DEST_FILE'].replace(
                                '$Author', authorname).replace(
                                '$Title', bookname).replace(
                                '$Series', '').replace(
                                '$SerName', '').replace(
                                '$SerNum', '').replace(
                                '$$', ' ')
                            global_name = ' '.join(global_name.split()).strip()
                        else:
                            data = myDB.match('SELECT IssueDate from magazines WHERE Title=?', (book['BookID'],))
                            if data:  # it's a magazine
                                logger.debug('Processing magazine %s' % book['BookID'])
                                # AuxInfo was added for magazine release date, normally housed in 'magazines'
                                # but if multiple files are downloading, there will be an error in post-processing
                                # trying to go to the same directory.
                                mostrecentissue = data['IssueDate']  # keep for processing issues arriving out of order
                                mag_name = unaccented_str(replace_all(book['BookID'], __dic__))
                                # book auxinfo is a cleaned date, eg 2015-01-01
                                dest_path = lazylibrarian.CONFIG['MAG_DEST_FOLDER'].replace(
                                    '$IssueDate', book['AuxInfo']).replace('$Title', mag_name)

                                if lazylibrarian.CONFIG['MAG_RELATIVE']:
                                    if dest_path[0] not in '._':
                                        dest_path = '_' + dest_path
                                    dest_dir = lazylibrarian.DIRECTORY('eBook')
                                    dest_path = os.path.join(dest_dir, dest_path)

                                if PY2:
                                    dest_path = dest_path.encode(lazylibrarian.SYS_ENCODING)
                                authorname = None
                                bookname = None
                                global_name = lazylibrarian.CONFIG['MAG_DEST_FILE'].replace(
                                    '$IssueDate', book['AuxInfo']).replace('$Title', mag_name)
                                global_name = unaccented(global_name)
                            else:  # not recognised, maybe deleted
                                logger.debug('Nothing in database matching "%s"' % book['BookID'])
                                controlValueDict = {"BookID": book['BookID'], "Status": "Snatched"}
                                newValueDict = {"Status": "Failed", "NZBDate": now()}
                                myDB.upsert("wanted", newValueDict, controlValueDict)
                                continue
                    else:
                        logger.debug("Snatched %s %s is not in download directory" %
                                     (book['NZBmode'], book['NZBtitle']))
                        if match:
                            logger.debug('Closest match (%s%%): %s' % (match, pp_path))
                            if int(lazylibrarian.LOGLEVEL) > 2:
                                for match in matches:
                                    logger.debug('Match: %s%%  %s' % (match[0], match[1]))
                        continue

                    success, dest_file = processDestination(pp_path, dest_path, authorname, bookname,
                                                            global_name, book['BookID'], book_type)
                    if success:
                        logger.debug("Processed %s: %s, %s" % (book['NZBmode'], global_name, book['NZBurl']))
                        # only update the snatched ones in case multiple matches for same book/magazine issue
                        controlValueDict = {"NZBurl": book['NZBurl'], "Status": "Snatched"}
                        newValueDict = {"Status": "Processed", "NZBDate": now()}  # say when we processed it
                        myDB.upsert("wanted", newValueDict, controlValueDict)

                        if bookname:  # it's ebook or audiobook
                            processExtras(dest_file, global_name, book['BookID'], book_type)
                        else:  # update mags
                            if mostrecentissue:
                                if mostrecentissue.isdigit() and str(book['AuxInfo']).isdigit():
                                    older = (int(mostrecentissue) > int(book['AuxInfo']))  # issuenumber
                                else:
                                    older = (mostrecentissue > book['AuxInfo'])  # YYYY-MM-DD
                            else:
                                older = False

                            controlValueDict = {"Title": book['BookID']}
                            if older:  # check this in case processing issues arriving out of order
                                newValueDict = {"LastAcquired": today(), "IssueStatus": "Open"}
                            else:
                                newValueDict = {"IssueDate": book['AuxInfo'], "LastAcquired": today(),
                                                "LatestCover": os.path.splitext(dest_file)[0] + '.jpg',
                                                "IssueStatus": "Open"}
                            myDB.upsert("magazines", newValueDict, controlValueDict)

                            iss_id = create_id("%s %s" % (book['BookID'], book['AuxInfo']))
                            controlValueDict = {"Title": book['BookID'], "IssueDate": book['AuxInfo']}
                            newValueDict = {"IssueAcquired": today(),
                                            "IssueFile": dest_file,
                                            "IssueID": iss_id
                                            }
                            myDB.upsert("issues", newValueDict, controlValueDict)

                            # create a thumbnail cover for the new issue
                            create_cover(dest_file)
                            processMAGOPF(dest_file, book['BookID'], book['AuxInfo'], iss_id)
                            if lazylibrarian.CONFIG['IMP_AUTOADDMAG']:
                                dest_path = os.path.dirname(dest_file)
                                processAutoAdd(dest_path, booktype='mag')

                        # calibre or ll copied/moved the files we want, now delete source files

                        to_delete = True
                        if book['NZBmode'] in ['torrent', 'magnet', 'torznab']:
                            # Only delete torrents if we don't want to keep seeding
                            if lazylibrarian.CONFIG['KEEP_SEEDING']:
                                logger.warn('%s is seeding %s %s' % (book['Source'], book['NZBmode'], book['NZBtitle']))
                                to_delete = False

                        if to_delete:
                            # ask downloader to delete the torrent, but not the files
                            # we may delete them later, depending on other settings
                            if not book['Source']:
                                logger.warn("Unable to remove %s, no source" % book['NZBtitle'])
                            elif not book['DownloadID'] or book['DownloadID'] == "unknown":
                                logger.warn("Unable to remove %s from %s, no DownloadID" %
                                            (book['NZBtitle'], book['Source'].lower()))
                            elif book['Source'] != 'DIRECT':
                                logger.debug('Removing %s from %s' % (book['NZBtitle'], book['Source'].lower()))
                                delete_task(book['Source'], book['DownloadID'], False)

                        if to_delete:
                            # only delete the files if not in download root dir and DESTINATION_COPY not set
                            # always delete files we unpacked from an archive
                            if lazylibrarian.CONFIG['DESTINATION_COPY']:
                                to_delete = False
                            if pp_path == download_dir:
                                to_delete = False
                            if pp_path.endswith('.unpack'):
                                to_delete = True
                            if to_delete:
                                if os.path.isdir(pp_path):
                                    # calibre might have already deleted it?
                                    try:
                                        shutil.rmtree(pp_path)
                                        logger.debug('Deleted %s, %s from %s' %
                                                     (book['NZBtitle'], book['NZBmode'], book['Source'].lower()))
                                    except Exception as why:
                                        logger.debug("Unable to remove %s, %s %s" %
                                                     (pp_path, type(why).__name__, str(why)))
                            else:
                                if lazylibrarian.CONFIG['DESTINATION_COPY']:
                                    logger.debug("Not removing original files as Keep Files is set")
                                else:
                                    logger.debug("Not removing original files as in download root")

                        logger.info('Successfully processed: %s' % global_name)

                        ppcount += 1
                        if bookname:
                            custom_notify_download(book['BookID'])
                            notify_download("%s %s from %s at %s" %
                                            (book_type, global_name, book['NZBprov'], now()), book['BookID'])
                        else:
                            custom_notify_download(iss_id)
                            notify_download("%s %s from %s at %s" %
                                            (book_type, global_name, book['NZBprov'], now()), iss_id)

                        update_downloads(book['NZBprov'])
                    else:
                        logger.error('Postprocessing for %s has failed: %s' % (global_name, dest_file))
                        controlValueDict = {"NZBurl": book['NZBurl'], "Status": "Snatched"}
                        newValueDict = {"Status": "Failed", "NZBDate": now()}
                        myDB.upsert("wanted", newValueDict, controlValueDict)
                        # if it's a book, reset status so we try for a different version
                        # if it's a magazine, user can select a different one from pastissues table
                        if book_type == 'eBook':
                            myDB.action('UPDATE books SET status="Wanted" WHERE BookID=?', (book['BookID'],))
                        elif book_type == 'AudioBook':
                            myDB.action('UPDATE books SET audiostatus="Wanted" WHERE BookID=?', (book['BookID'],))

                        # at this point, as it failed we should move it or it will get postprocessed
                        # again (and fail again)
                        if os.path.isdir(pp_path + '.fail'):
                            try:
                                shutil.rmtree(pp_path + '.fail')
                            except Exception as why:
                                logger.debug("Unable to remove %s, %s %s" %
                                             (pp_path + '.fail', type(why).__name__, str(why)))
                        try:
                            shutil.move(pp_path, pp_path + '.fail')
                            logger.warn('Residual files remain in %s.fail' % pp_path)
                        except Exception as why:
                            logger.error("[processDir] Unable to rename %s, %s %s" %
                                         (pp_path, type(why).__name__, str(why)))
                            logger.warn('Residual files remain in %s' % pp_path)

            # Check for any books in download that weren't marked as snatched, but have a LL.(bookid)
            # do a fresh listdir in case we processed and deleted any earlier
            # and don't process any we've already done as we might not want to delete originals
            downloads = os.listdir(makeBytestr(download_dir))
            downloads = [makeUnicode(item) for item in downloads]
            if int(lazylibrarian.LOGLEVEL) > 2:
                logger.debug("Scanning %s entries in %s for LL.(num)" % (len(downloads), download_dir))
            for entry in downloads:
                if "LL.(" in entry:
                    dname, extn = os.path.splitext(entry)
                    if not extn or extn not in skipped_extensions:
                        bookID = entry.split("LL.(")[1].split(")")[0]
                        logger.debug("Book with id: %s found in download directory" % bookID)
                        data = myDB.match('SELECT BookFile from books WHERE BookID=?', (bookID,))
                        if data and data['BookFile'] and os.path.isfile(data['BookFile']):
                            logger.debug('Skipping BookID %s, already exists' % bookID)
                        else:
                            pp_path = os.path.join(download_dir, entry)

                            if int(lazylibrarian.LOGLEVEL) > 2:
                                logger.debug("Checking type of %s" % pp_path)

                            if os.path.isfile(pp_path):
                                if int(lazylibrarian.LOGLEVEL) > 2:
                                    logger.debug("%s is a file" % pp_path)
                                pp_path = os.path.join(download_dir)

                            if os.path.isdir(pp_path):
                                if int(lazylibrarian.LOGLEVEL) > 2:
                                    logger.debug("%s is a dir" % pp_path)
                                if import_book(pp_path, bookID):
                                    if int(lazylibrarian.LOGLEVEL) > 2:
                                        logger.debug("Imported %s" % pp_path)
                                    ppcount += 1
                    else:
                        if int(lazylibrarian.LOGLEVEL) > 2:
                            logger.debug("Skipping extn %s" % entry)
                else:
                    if int(lazylibrarian.LOGLEVEL) > 2:
                        logger.debug("Skipping (not LL) %s" % entry)

        logger.info('%s book%s/mag%s processed.' % (ppcount, plural(ppcount), plural(ppcount)))

        # Now check for any that are still marked snatched...
        snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
        if lazylibrarian.CONFIG['TASK_AGE'] and len(snatched) > 0:
            for book in snatched:
                book_type = book['AuxInfo']
                if book_type != 'AudioBook' and book_type != 'eBook':
                    if book_type is None or book_type == '':
                        book_type = 'eBook'
                    else:
                        book_type = 'Magazine'
                # FUTURE: we could check percentage downloaded or eta?
                # if percentage is increasing, it's just slow
                try:
                    when_snatched = time.strptime(book['NZBdate'], '%Y-%m-%d %H:%M:%S')
                    when_snatched = time.mktime(when_snatched)
                    diff = time.time() - when_snatched  # time difference in seconds
                except ValueError:
                    diff = 0
                hours = int(diff / 3600)
                if hours >= lazylibrarian.CONFIG['TASK_AGE']:
                    if book['Source'] and book['Source'] != 'DIRECT':
                        logger.warn('%s was sent to %s %s hours ago, deleting failed task' %
                                    (book['NZBtitle'], book['Source'].lower(), hours))
                    # change status to "Failed", and ask downloader to delete task and files
                    # Only reset book status to wanted if still snatched in case another download task succeeded
                    if book['BookID'] != 'unknown':
                        cmd = ''
                        if book_type == 'eBook':
                            cmd = 'UPDATE books SET status="Wanted" WHERE status="Snatched" and BookID=?'
                        elif book_type == 'AudioBook':
                            cmd = 'UPDATE books SET audiostatus="Wanted" WHERE audiostatus="Snatched" and BookID=?'
                        if cmd:
                            myDB.action(cmd, (book['BookID'],))
                        myDB.action('UPDATE wanted SET Status="Failed" WHERE BookID=?', (book['BookID'],))
                        delete_task(book['Source'], book['DownloadID'], True)

        # Check if postprocessor needs to run again
        snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
        if len(snatched) == 0:
            logger.info('Nothing marked as snatched. Stopping postprocessor.')
            scheduleJob(action='Stop', target='processDir')

        if reset:
            scheduleJob(action='Restart', target='processDir')

    except Exception:
        logger.error('Unhandled exception in processDir: %s' % traceback.format_exc())

    finally:
        threading.currentThread().name = "WEBSERVER"


def delete_task(Source, DownloadID, remove_data):
    try:
        if Source == "BLACKHOLE":
            logger.warn("Download %s has not been processed from blackhole" % DownloadID)
        elif Source == "SABNZBD":
            sabnzbd.SABnzbd(DownloadID, 'delete', remove_data)
            sabnzbd.SABnzbd(DownloadID, 'delhistory', remove_data)
        elif Source == "NZBGET":
            nzbget.deleteNZB(DownloadID, remove_data)
        elif Source == "UTORRENT":
            utorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "RTORRENT":
            rtorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "QBITTORRENT":
            qbittorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "TRANSMISSION":
            transmission.removeTorrent(DownloadID, remove_data)
        elif Source == "SYNOLOGY_TOR" or Source == "SYNOLOGY_NZB":
            synology.removeTorrent(DownloadID, remove_data)
        elif Source == "DELUGEWEBUI":
            deluge.removeTorrent(DownloadID, remove_data)
        elif Source == "DELUGERPC":
            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'],
                                     lazylibrarian.CONFIG['DELUGE_URL_BASE'],
                                     int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                     lazylibrarian.CONFIG['DELUGE_USER'],
                                     lazylibrarian.CONFIG['DELUGE_PASS'])
            try:
                client.connect()
                client.call('core.remove_torrent', DownloadID, remove_data)
            except Exception as e:
                logger.debug('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))
                return False
        elif Source == 'DIRECT':
            return True
        else:
            logger.debug("Unknown source [%s] in delete_task" % Source)
            return False
        return True

    except Exception as e:
        logger.debug("Failed to delete task %s from %s: %s %s" % (DownloadID, Source, type(e).__name__, str(e)))
        return False


def import_book(pp_path=None, bookID=None):
    # noinspection PyBroadException
    try:
        # Move a book into LL folder structure given just the folder and bookID, returns True or False
        # Called from "import_alternate" or if we find a "LL.(xxx)" folder that doesn't match a snatched book/mag
        if int(lazylibrarian.LOGLEVEL) > 2:
            logger.debug("import_book %s" % pp_path)
        if book_file(pp_path, "audiobook"):
            book_type = "AudioBook"
            dest_dir = lazylibrarian.DIRECTORY('Audio')
        elif book_file(pp_path, "ebook"):
            book_type = "eBook"
            dest_dir = lazylibrarian.DIRECTORY('eBook')
        else:
            logger.warn("Failed to find an ebook or audiobook in [%s]" % pp_path)
            return False

        myDB = database.DBConnection()
        cmd = 'SELECT AuthorName,BookName from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
        data = myDB.match(cmd, (bookID,))
        if data:
            cmd = 'SELECT BookID, NZBprov, AuxInfo FROM wanted WHERE BookID=? and Status="Snatched"'
            # we may have wanted to snatch an ebook and audiobook of the same title/id
            was_snatched = myDB.select(cmd, (bookID,))
            want_audio = False
            want_ebook = False
            for item in was_snatched:
                if item['AuxInfo'] == 'AudioBook':
                    want_audio = True
                elif item['AuxInfo'] == 'eBook' or item['AuxInfo'] == '':
                    want_ebook = True

            match = False
            if want_audio and book_type == "AudioBook":
                match = True
            elif want_ebook and book_type == "eBook":
                match = True
            elif not was_snatched:
                logger.debug('Bookid %s was not snatched so cannot check type, contains %s' % (bookID, book_type))
                match = True
            if not match:
                logger.debug('Bookid %s, failed to find valid %s' % (bookID, book_type))
                return False

            authorname = data['AuthorName']
            authorname = ' '.join(authorname.split())  # ensure no extra whitespace
            bookname = data['BookName']
            # DEST_FOLDER pattern is the same for ebook and audiobook
            if 'windows' in platform.system().lower() and '/' in lazylibrarian.CONFIG['EBOOK_DEST_FOLDER']:
                logger.warn('Please check your EBOOK_DEST_FOLDER setting')
                lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'] = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace('/', '\\')

            dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
                            '$Author', authorname).replace(
                            '$Title', bookname).replace(
                            '$Series', seriesInfo(bookID)).replace(
                            '$SerName', seriesInfo(bookID, 'Name')).replace(
                            '$SerNum', seriesInfo(bookID, 'Num')).replace(
                            '$$', ' ')
            dest_path = ' '.join(dest_path.split()).strip()
            dest_path = replace_all(dest_path, __dic__)
            dest_path = os.path.join(dest_dir, dest_path)
            # global_name is only used for ebooks to ensure book/cover/opf all have the same basename
            # audiobooks are usually multi part so can't be renamed this way
            global_name = lazylibrarian.CONFIG['EBOOK_DEST_FILE'].replace(
                '$Author', authorname).replace(
                '$Title', bookname).replace(
                '$Series', '').replace(
                '$SerName', '').replace(
                '$SerNum', '').replace(
                '$$', ' ')
            global_name = ' '.join(global_name.split()).strip()

            if int(lazylibrarian.LOGLEVEL) > 2:
                logger.debug("processDestination %s" % pp_path)

            success, dest_file = processDestination(pp_path, dest_path, authorname, bookname,
                                                    global_name, bookID, book_type)
            if success:
                # update nzbs
                if was_snatched:
                    snatched_from = was_snatched[0]['NZBprov']
                    if int(lazylibrarian.LOGLEVEL) > 2:
                        logger.debug("%s was snatched from %s" % (global_name, snatched_from))
                    controlValueDict = {"BookID": bookID}
                    newValueDict = {"Status": "Processed", "NZBDate": now()}  # say when we processed it
                    myDB.upsert("wanted", newValueDict, controlValueDict)
                else:
                    snatched_from = "manually added"
                    if int(lazylibrarian.LOGLEVEL) > 2:
                        logger.debug("%s was %s" % (global_name, snatched_from))

                processExtras(dest_file, global_name, bookID, book_type)

                if not lazylibrarian.CONFIG['DESTINATION_COPY'] and pp_path != dest_dir:
                    if os.path.isdir(pp_path):
                        # calibre might have already deleted it?
                        try:
                            shutil.rmtree(pp_path)
                        except Exception as why:
                            logger.debug("Unable to remove %s, %s %s" % (pp_path, type(why).__name__, str(why)))
                else:
                    if lazylibrarian.CONFIG['DESTINATION_COPY']:
                        logger.debug("Not removing original files as Keep Files is set")
                    else:
                        logger.debug("Not removing original files as in download root")

                logger.info('Successfully processed: %s' % global_name)
                custom_notify_download(bookID)
                if snatched_from == "manually added":
                    frm = ''
                else:
                    frm = 'from '

                notify_download("%s %s %s%s at %s" % (book_type, global_name, frm, snatched_from, now()), bookID)
                update_downloads(snatched_from)
                return True
            else:
                logger.error('Postprocessing for %s has failed: %s' % (global_name, dest_file))
                if os.path.isdir(pp_path + '.fail'):
                    try:
                        shutil.rmtree(pp_path + '.fail')
                    except Exception as why:
                        logger.debug("Unable to remove %s, %s %s" % (pp_path + '.fail', type(why).__name__, str(why)))
                try:
                    shutil.move(pp_path, pp_path + '.fail')
                    logger.warn('Residual files remain in %s.fail' % pp_path)
                except Exception as e:
                    logger.error("[importBook] Unable to rename %s, %s %s" % (pp_path, type(e).__name__, str(e)))
                    logger.warn('Residual files remain in %s' % pp_path)

                was_snatched = myDB.match('SELECT NZBurl FROM wanted WHERE BookID=? and Status="Snatched"', (bookID,))
                if was_snatched:
                    controlValueDict = {"NZBurl": was_snatched['NZBurl']}
                    newValueDict = {"Status": "Failed", "NZBDate": now()}
                    myDB.upsert("wanted", newValueDict, controlValueDict)
                # reset status so we try for a different version
                if book_type == 'AudioBook':
                    myDB.action('UPDATE books SET audiostatus="Wanted" WHERE BookID=?', (bookID,))
                else:
                    myDB.action('UPDATE books SET status="Wanted" WHERE BookID=?', (bookID,))
        return False
    except Exception:
        logger.error('Unhandled exception in importBook: %s' % traceback.format_exc())


def processExtras(dest_file=None, global_name=None, bookid=None, book_type="eBook"):
    # given bookid, handle author count, calibre autoadd, book image, opf

    if not bookid:
        logger.error('processExtras: No bookid supplied')
        return
    if not dest_file:
        logger.error('processExtras: No dest_file supplied')
        return

    myDB = database.DBConnection()

    controlValueDict = {"BookID": bookid}
    if book_type == 'AudioBook':
        newValueDict = {"AudioFile": dest_file, "AudioStatus": "Open", "AudioLibrary": now()}
        myDB.upsert("books", newValueDict, controlValueDict)
        if lazylibrarian.CONFIG['AUDIOBOOK_DEST_FILE'] and lazylibrarian.CONFIG['IMP_RENAME']:
            book_filename = audioRename(bookid)
            if dest_file != book_filename:
                myDB.action('UPDATE books set AudioFile=? where BookID=?', (book_filename, bookid))
    else:
        newValueDict = {"Status": "Open", "BookFile": dest_file, "BookLibrary": now()}
        myDB.upsert("books", newValueDict, controlValueDict)

    # update authors book counts
    match = myDB.match('SELECT AuthorID FROM books WHERE BookID=?', (bookid,))
    if match:
        update_totals(match['AuthorID'])

    elif book_type != 'eBook':  # only do autoadd/img/opf for ebooks
        return

    cmd = 'SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub'
    cmd += ' from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
    data = myDB.match(cmd, (bookid,))
    if not data:
        logger.error('processExtras: No data found for bookid %s' % bookid)
        return

    dest_path = os.path.dirname(dest_file)

    # download and cache image if http link
    processIMG(dest_path, data['BookID'], data['BookImg'], global_name)

    # do we want to create metadata - there may already be one in pp_path, but it was downloaded and might
    # not contain our choice of authorname/title/identifier, so we ignore it and write our own
    if not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
        _ = processOPF(dest_path, data, global_name, overwrite=True)

    # If you use auto add by Calibre you need the book in a single directory, not nested
    # So take the files you Copied/Moved to Dest_path and copy/move into Calibre auto add folder.
    if lazylibrarian.CONFIG['IMP_AUTOADD']:
        processAutoAdd(dest_path)


def processDestination(pp_path=None, dest_path=None, authorname=None, bookname=None, global_name=None, bookid=None,
                       booktype=None):
    """ Copy/move book/mag and associated files into target directory
        Return True, full_path_to_book  or False, error_message"""

    if not bookname:
        booktype = 'mag'

    booktype = booktype.lower()

    bestmatch = ''
    if booktype == 'ebook' and lazylibrarian.CONFIG['ONE_FORMAT']:
        booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
        for btype in booktype_list:
            if not bestmatch:
                for fname in os.listdir(makeBytestr(pp_path)):
                    fname = makeUnicode(fname)
                    extn = os.path.splitext(fname)[1].lstrip('.')
                    if extn and extn.lower() == btype:
                        bestmatch = btype
                        break
    if bestmatch:
        match = bestmatch
        logger.debug('One format import, best match = %s' % bestmatch)
    else:  # mag or audiobook or multi-format book
        match = False
        for fname in os.listdir(makeBytestr(pp_path)):
            fname = makeUnicode(fname)
            if is_valid_booktype(fname, booktype=booktype):
                match = True
                break

    if not match:
        # no book/mag found in a format we wanted. Leave for the user to delete or convert manually
        return False, 'Unable to locate a valid filetype (%s) in %s, leaving for manual processing' % (
            booktype, pp_path)

    # If ebook, do we want calibre to import the book for us
    newbookfile = ''
    if booktype == 'ebook' and len(lazylibrarian.CONFIG['IMP_CALIBREDB']):
        dest_dir = lazylibrarian.DIRECTORY('eBook')
        try:
            logger.debug('Importing %s into calibre library' % global_name)
            # calibre may ignore metadata.opf and book_name.opf depending on calibre settings,
            # and ignores opf data if there is data embedded in the book file
            # so we send separate "set_metadata" commands after the import
            for fname in os.listdir(makeBytestr(pp_path)):
                fname = makeUnicode(fname)
                if bestmatch and is_valid_booktype(fname, booktype=booktype) and not fname.endswith(bestmatch):
                    logger.debug("Ignoring %s as not %s" % (fname, bestmatch))
                else:
                    filename, extn = os.path.splitext(fname)
                    # calibre does not like quotes in author names
                    if lazylibrarian.CONFIG['DESTINATION_COPY']:
                        shutil.copyfile(os.path.join(pp_path, filename + extn), os.path.join(
                            pp_path, global_name.replace('"', '_') + extn))
                    else:
                        shutil.move(os.path.join(pp_path, filename + extn), os.path.join(
                            pp_path, global_name.replace('"', '_') + extn))

            if bookid.isdigit():
                identifier = "goodreads:%s" % bookid
            else:
                identifier = "google:%s" % bookid

            res, err, rc = calibredb('add', ['-1'], [pp_path])

            if res:
                logger.debug('%s result : %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(res)))
            if err:
                logger.debug('%s error  : %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(err)))

            if rc or not res:
                return False, 'calibredb rc %s from %s' % (rc, lazylibrarian.CONFIG['IMP_CALIBREDB'])
            elif 'already exist' in err or 'already exist' in res:  # needed for different calibredb versions
                return False, 'Calibre failed to import %s %s, already exists' % (authorname, bookname)
            elif 'Added book ids' not in res:
                return False, 'Calibre failed to import %s %s, no added bookids' % (authorname, bookname)

            calibre_id = res.split("book ids: ", 1)[1].split("\n", 1)[0]
            logger.debug('Calibre ID: %s' % calibre_id)

            our_opf = False
            if not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
                # we can pass an opf with all the info, and a cover image
                myDB = database.DBConnection()
                cmd = 'SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub'
                cmd += ' from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
                data = myDB.match(cmd, (bookid,))
                if not data:
                    logger.error('processDestination: No data found for bookid %s' % bookid)
                else:
                    processIMG(pp_path, data['BookID'], data['BookImg'], global_name)
                    opfpath, our_opf = processOPF(pp_path, data, global_name, True)
                    res, err, rc = calibredb('set_metadata', None, [calibre_id, opfpath])
                    if res and not rc:
                        logger.debug(
                            '%s set opf reports: %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(res)))

            if not our_opf:  # pre-existing opf might not have our preferred authorname/title/identifier
                res, err, rc = calibredb('set_metadata', ['--field', 'authors:%s' % unaccented(authorname)],
                                         [calibre_id])
                if res and not rc:
                    logger.debug(
                        '%s set author reports: %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(res)))

                res, err, rc = calibredb('set_metadata', ['--field', 'title:%s' % unaccented(bookname)], [calibre_id])
                if res and not rc:
                    logger.debug(
                        '%s set title reports: %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(res)))

                res, err, rc = calibredb('set_metadata', ['--field', 'identifiers:%s' % identifier], [calibre_id])
                if res and not rc:
                    logger.debug(
                        '%s set identifier reports: %s' % (lazylibrarian.CONFIG['IMP_CALIBREDB'], unaccented_str(res)))

            # calibre does not like accents or quotes in names
            if authorname.endswith('.'):  # calibre replaces trailing dot with underscore eg Jr. becomes Jr_
                authorname = authorname[:-1] + '_'
            calibre_dir = os.path.join(dest_dir, unaccented_str(authorname.replace('"', '_')), '')
            if os.path.isdir(calibre_dir):  # assumed author directory
                target_dir = os.path.join(calibre_dir, '%s (%s)' % (unaccented(bookname), calibre_id))
                logger.debug('Calibre trying directory [%s]' % target_dir)
                remove = bool(lazylibrarian.CONFIG['FULL_SCAN'])
                if os.path.isdir(target_dir):
                    _ = LibraryScan(target_dir, remove=remove)
                    newbookfile = book_file(target_dir, booktype='ebook')
                    if newbookfile:
                        setperm(target_dir)
                        for fname in os.listdir(makeBytestr(target_dir)):
                            fname = makeUnicode(fname)
                            setperm(os.path.join(target_dir, fname))
                        return True, newbookfile
                    return False, "Failed to find a valid ebook in [%s]" % target_dir
                else:
                    _ = LibraryScan(calibre_dir, remove=remove)  # rescan whole authors directory
                    myDB = database.DBConnection()
                    match = myDB.match('SELECT BookFile FROM books WHERE BookID=?', (bookid,))
                    if match:
                        return True, match['BookFile']
                    return False, 'Failed to find bookfile for %s in database' % bookid
            return False, "Failed to locate calibre author dir [%s]" % calibre_dir
            # imported = LibraryScan(dest_dir)  # may have to rescan whole library instead
        except Exception as e:
            return False, 'calibredb import failed, %s %s' % (type(e).__name__, str(e))
    else:
        # we are copying the files ourselves, either it's audiobook, magazine or we don't want to use calibre
        logger.debug("BookType: %s, calibredb: [%s]" % (booktype, lazylibrarian.CONFIG['IMP_CALIBREDB']))
        if not os.path.exists(dest_path):
            logger.debug('%s does not exist, so it\'s safe to create it' % dest_path)
        elif not os.path.isdir(dest_path):
            logger.debug('%s exists but is not a directory, deleting it' % dest_path)
            try:
                os.remove(dest_path)
            except OSError as why:
                return False, 'Unable to delete %s: %s' % (dest_path, why.strerror)
        try:
            os.makedirs(dest_path)
            setperm(dest_path)
        except OSError as why:
            if not os.path.isdir(dest_path):
                return False, 'Unable to create directory %s: %s' % (dest_path, why.strerror)

        # ok, we've got a target directory, try to copy only the files we want, renaming them on the fly.
        firstfile = ''  # try to keep track of "preferred" ebook type or the first part of multi-part audiobooks
        for fname in os.listdir(makeBytestr(pp_path)):
            fname = makeUnicode(fname)
            if bestmatch and is_valid_booktype(fname, booktype=booktype) and not fname.endswith(bestmatch):
                logger.debug("Ignoring %s as not %s" % (fname, bestmatch))
            else:
                if is_valid_booktype(fname, booktype=booktype) or \
                        ((fname.lower().endswith(".jpg") or fname.lower().endswith(".opf"))
                         and not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']):
                    logger.debug('Copying %s to directory %s' % (fname, dest_path))
                    typ = ''
                    srcfile = os.path.join(pp_path, fname)
                    if booktype == 'audiobook':
                        destfile = os.path.join(dest_path, fname)  # don't rename, just copy it
                    else:
                        # for ebooks, the book, jpg, opf all have the same basename
                        destfile = os.path.join(dest_path, global_name + os.path.splitext(fname)[1])
                    try:
                        if lazylibrarian.CONFIG['DESTINATION_COPY']:
                            typ = 'copy'
                            shutil.copyfile(srcfile, destfile)
                        else:
                            typ = 'move'
                            shutil.move(srcfile, destfile)
                        setperm(destfile)
                        if is_valid_booktype(destfile, booktype=booktype):
                            newbookfile = destfile
                    except Exception as why:
                        return False, "Unable to %s file %s to %s: %s %s" % \
                               (typ, srcfile, destfile, type(why).__name__, str(why))
                else:
                    logger.debug('Ignoring unwanted file: %s' % fname)

        # for ebooks, prefer the first book_type found in ebook_type list
        if booktype == 'ebook':
            book_basename = os.path.join(dest_path, global_name)
            booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
            for book_type in booktype_list:
                preferred_type = "%s.%s" % (book_basename, book_type)
                if os.path.exists(preferred_type):
                    logger.debug("Link to preferred type %s, %s" % (book_type, preferred_type))
                    firstfile = preferred_type
                    break

        # link to the first part of multi-part audiobooks
        elif booktype == 'audiobook':
            tokmatch = ''
            for token in [' 001.', ' 01.', ' 1.', ' 001 ', ' 01 ', ' 1 ', '01']:
                if tokmatch:
                    break
                for f in os.listdir(makeBytestr(pp_path)):
                    f = makeUnicode(f)
                    if is_valid_booktype(f, booktype='audiobook') and token in f:
                        firstfile = os.path.join(pp_path, f)
                        logger.debug("Link to preferred part [%s], %s" % (token, f))
                        tokmatch = token
                        break
        if firstfile:
            newbookfile = firstfile
    return True, newbookfile


def processAutoAdd(src_path=None, booktype='book'):
    # Called to copy/move the book files to an auto add directory for the likes of Calibre which can't do nested dirs
    autoadddir = lazylibrarian.CONFIG['IMP_AUTOADD']
    if booktype == 'mag':
        autoadddir = lazylibrarian.CONFIG['IMP_AUTOADDMAG']

    if not os.path.exists(autoadddir):
        logger.error('AutoAdd directory for %s [%s] is missing or not set - cannot perform autoadd' % (
                      booktype, autoadddir))
        return False
    # Now try and copy all the book files into a single dir.
    try:
        names = os.listdir(makeBytestr(src_path))
        names = [makeUnicode(item) for item in names]
        # files jpg, opf & book(s) should have same name
        # Caution - book may be pdf, mobi, epub or all 3.
        # for now simply copy all files, and let the autoadder sort it out
        #
        # Update - seems Calibre will only use the jpeg if named same as book, not cover.jpg
        # and only imports one format of each ebook, treats the others as duplicates, might be configable in calibre?
        # ignores author/title data in opf file if there is any embedded in book

        match = False
        if booktype == 'book' and lazylibrarian.CONFIG['ONE_FORMAT']:
            booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
            for booktype in booktype_list:
                while not match:
                    for name in names:
                        extn = os.path.splitext(name)[1].lstrip('.')
                        if extn and extn.lower() == booktype:
                            match = booktype
                            break
        copied = False
        for name in names:
            if match and is_valid_booktype(name, booktype=booktype) and not name.endswith(match):
                logger.debug('Skipping %s' % os.path.splitext(name)[1])
            elif booktype == 'book' and lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY'] and not \
                    is_valid_booktype(name, booktype="book"):
                logger.debug('Skipping %s' % name)
            elif booktype == 'mag' and lazylibrarian.CONFIG['IMP_AUTOADD_MAGONLY'] and not \
                    is_valid_booktype(name, booktype="mag"):
                logger.debug('Skipping %s' % name)
            else:
                srcname = os.path.join(src_path, name)
                dstname = os.path.join(autoadddir, name)
                try:
                    if lazylibrarian.CONFIG['DESTINATION_COPY']:
                        logger.debug('AutoAdd Copying file [%s] from [%s] to [%s]' % (name, srcname, dstname))
                        shutil.copyfile(srcname, dstname)
                    else:
                        logger.debug('AutoAdd Moving file [%s] from [%s] to [%s]' % (name, srcname, dstname))
                        shutil.move(srcname, dstname)
                    copied = True
                except Exception as why:
                    logger.error('AutoAdd - Failed to copy/move file [%s] %s [%s] ' %
                                 (name, type(why).__name__, str(why)))
                    return False
                try:
                    os.chmod(dstname, 0o666)  # make rw for calibre
                except OSError as why:
                    logger.warn("Could not set permission of %s because [%s]" % (dstname, why.strerror))
                    # permissions might not be fatal, continue

        if copied and not lazylibrarian.CONFIG['DESTINATION_COPY']:  # do we want to keep the original files?
            logger.debug('Removing %s' % src_path)
            shutil.rmtree(src_path)

    except OSError as why:
        logger.error('AutoAdd - Failed because [%s]' % why.strerror)
        return False

    logger.info('Auto Add completed for [%s]' % src_path)
    return True


def processIMG(dest_path=None, bookid=None, bookimg=None, global_name=None):
    """ cache the bookimg from url or filename, and optionally copy it to bookdir """
    if lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
        logger.debug('Not creating coverfile, bookonly is set')
        return

    jpgfile = jpg_file(dest_path)
    if jpgfile:
        logger.debug('Cover %s already exists' % jpgfile)
        return

    link, success = cache_img('book', bookid, bookimg, False)
    if not success:
        logger.error('Error caching cover from %s, %s' % (bookimg, link))
        return

    cachefile = os.path.join(lazylibrarian.CACHEDIR, 'book', bookid + '.jpg')
    coverfile = os.path.join(dest_path, global_name + '.jpg')
    try:
        shutil.copyfile(cachefile, coverfile)
    except Exception as e:
        logger.debug("Error copying image to %s, %s %s" % (coverfile, type(e).__name__, str(e)))
        return


def processMAGOPF(issuefile, title, issue, issueID):
    """ Needs calibre to be configured to read metadata from file contents, not filename """
    if not lazylibrarian.CONFIG['IMP_MAGOPF']:
        return
    dest_path, global_name = os.path.split(issuefile)
    global_name, extn = os.path.splitext(global_name)

    if len(issue) == 10 and issue[8:] == '01' and issue[4] == '-' and issue[7] == '-':  # yyyy-mm-01
        yr = issue[0:4]
        mn = issue[5:7]
        month = lazylibrarian.MONTHNAMES[int(mn)][0]
        iname = "%s - %s%s %s" % (title, month[0].upper(), month[1:], yr)  # The Magpi - January 2017
    elif title in issue:
        iname = issue  # 0063 - Android Magazine -> 0063
    else:
        iname = "%s - %s" % (title, issue)  # Android Magazine - 0063

    mtime = os.path.getmtime(issuefile)
    iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

    data = {
        'AuthorName': title,
        'BookID': issueID,
        'BookName': iname,
        'BookDesc': '',
        'BookIsbn': '',
        'BookDate': iss_acquired,
        'BookLang': 'eng',
        'BookImg': global_name + '.jpg',
        'BookPub': '',
        'Series': title,
        'Series_index': issue
    }  # type: dict
    # noinspection PyTypeChecker
    _ = processOPF(dest_path, data, global_name, overwrite=True)


def processOPF(dest_path=None, data=None, global_name=None, overwrite=False):
    opfpath = os.path.join(dest_path, global_name + '.opf')
    if not overwrite and os.path.exists(opfpath):
        logger.debug('%s already exists. Did not create one.' % opfpath)
        return opfpath, False

    bookid = data['BookID']
    if bookid.isdigit():
        scheme = 'GOODREADS'
    else:
        scheme = 'GoogleBooks'

    seriesname = ''
    seriesnum = ''
    if 'Series_index' not in data:
        # no series details passed in data dictionary, look them up in db
        myDB = database.DBConnection()
        cmd = 'SELECT SeriesID,SeriesNum from member WHERE bookid=?'
        res = myDB.match(cmd, (bookid,))
        if res:
            seriesid = res['SeriesID']
            serieslist = getList(res['SeriesNum'])
            # might be "Book 3.5" or similar, just get the numeric part
            while serieslist:
                seriesnum = serieslist.pop()
                try:
                    _ = float(seriesnum)
                    break
                except ValueError:
                    seriesnum = ''
                    pass

            if not seriesnum:
                # couldn't figure out number, keep everything we got, could be something like "Book Two"
                serieslist = res['SeriesNum']

            cmd = 'SELECT SeriesName from series WHERE seriesid=?'
            res = myDB.match(cmd, (seriesid,))
            if res:
                seriesname = res['SeriesName']
                if not seriesnum:
                    # add what we got to series name and set seriesnum to 1 so user can sort it out manually
                    seriesname = "%s %s" % (seriesname, serieslist)
                    seriesnum = 1

    opfinfo = '<?xml version="1.0"  encoding="UTF-8"?>\n\
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" >\n\
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n\
        <dc:title>%s</dc:title>\n\
        <dc:creator opf:file-as="%s" opf:role="aut">%s</dc:creator>\n\
        <dc:language>%s</dc:language>\n\
        <dc:identifier scheme="%s">%s</dc:identifier>\n' % (data['BookName'], surnameFirst(data['AuthorName']),
                                                            data['AuthorName'], data['BookLang'], scheme, bookid)

    if 'BookIsbn' in data:
        opfinfo += '        <dc:identifier scheme="ISBN">%s</dc:identifier>\n' % data['BookIsbn']

    if 'BookPub' in data:
        opfinfo += '        <dc:publisher>%s</dc:publisher>\n' % data['BookPub']

    if 'BookDate' in data:
        opfinfo += '        <dc:date>%s</dc:date>\n' % data['BookDate']

    if 'BookDesc' in data:
        opfinfo += '        <dc:description>%s</dc:description>\n' % data['BookDesc']

    if seriesname:
        opfinfo += '        <meta content="%s" name="calibre:series"/>\n' % seriesname
    elif 'Series' in data:
        opfinfo += '        <meta content="%s" name="calibre:series"/>\n' % data['Series']

    if seriesnum:
        opfinfo += '        <meta content="%s" name="calibre:series_index"/>\n' % seriesnum
    elif 'Series_index' in data:
        opfinfo += '        <meta content="%s" name="calibre:series_index"/>\n' % data['Series_index']

    opfinfo += '        <guide>\n\
            <reference href="%s.jpg" type="cover" title="Cover"/>\n\
        </guide>\n\
    </metadata>\n\
</package>' % global_name  # file in current directory, not full path

    dic = {'...': '', ' & ': ' ', ' = ': ' ', '$': 's', ' + ': ' ', '*': ''}

    opfinfo = unaccented_str(replace_all(opfinfo, dic))

    if PY2:
        fmode = 'wb'
    else:
        fmode = 'w'
    with open(opfpath, fmode) as opf:
        opf.write(opfinfo)
    logger.debug('Saved metadata to: ' + opfpath)
    return opfpath, True
