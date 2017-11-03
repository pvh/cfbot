import gzip
import HTMLParser
import unicodedata
import urllib
import urllib2
import urlparse

from prep import *
from submission import *

# travis build settings that will be added to automatically generated branches
TRAVIS_FILE = """
sudo: required
addons:
  apt:
    packages:
      - gdb
      - lcov
      - libipc-run-perl
      - libperl-dev
      - libpython-dev
      - tcl-dev
      - libldap2-dev
      - libicu-dev
      - docbook
      - docbook-dsssl
      - docbook-xsl
      - libxml2-utils
      - openjade1.3
      - opensp
      - xsltproc
language: c
cache: ccache
before_install:
  - echo '/tmp/%e-%s-%p.core' | sudo tee /proc/sys/kernel/core_pattern
  - echo "deb http://archive.ubuntu.com/ubuntu xenial main" | sudo tee /etc/apt/sources.list.d/xenial.list > /dev/null
  - |
    sudo tee -a /etc/apt/preferences.d/trusty > /dev/null <<EOF
    Package: *
    Pin: release n=xenial
    Pin-Priority: 1
    
    Package: make
    Pin: release n=xenial
    Pin-Priority: 500
    EOF
  - sudo apt-get update && sudo apt-get install make
script: ./configure --enable-debug --enable-cassert --enable-coverage --enable-tap-tests --with-tcl --with-python --with-perl --with-ldap --with-icu && make -j4 all contrib docs && make -Otarget -j3 check-world
after_success:
  - bash <(curl -s https://codecov.io/bash)
after_failure:
  - for f in ` find . -name regression.diffs ` ; do echo "========= Contents of $f" ; head -1000 $f ; done
  - |
    for corefile in $(find /tmp/ -name '*.core' 2>/dev/null) ; do
      binary=$(gdb -quiet -core $corefile -batch -ex 'info auxv' | grep AT_EXECFN | perl -pe "s/^.*\\"(.*)\\"\$/\$1/g")
      echo dumping $corefile for $binary
      gdb --batch --quiet -ex "thread apply all bt full" -ex "quit" $binary $corefile
    done
"""


def get_thread_url_for_submission(commitfest_id, submission_id):
  """Given a commitfest ID and a submission ID, return the URL of the 'whole
     thread' page in the mailing list archives."""
  if submission_id == 951:
    # this one has two threads, and the interesting one is listed first (need to learn about dates?)
    return "https://www.postgresql.org/message-id/flat/CAEepm=1iiEzCVLD=RoBgtZSyEY1CR-Et7fRc9prCZ9MuTz3pWg@mail.gmail.com"
  elif submission_id == 994:
    # this one is truncated, and there is a new 'flat' URL for the continuation
    return "https://www.postgresql.org/message-id/flat/CAOGQiiN9m%3DKRf-et1T0AcimbyAB9hDzJqGkHnOBjWT4uF1z1BQ%40mail.gmail.com"
  # if there is more than one, we'll take the furthest down on the page...
  result = None
  url = "https://commitfest.postgresql.org/%s/%s/" % (commitfest_id, submission_id)
  for line in slow_fetch(url).splitlines():
    groups = re.search('<dt><a href="(https://www.postgresql.org/message-id/flat/[^"]+)"', line)
    if groups:
      result = groups.group(1)
  return result
  
def get_submissions_for_commitfest(commitfest_id):
  """Given a commitfest ID, return a list of Submission objects."""
  result = []
  parser = HTMLParser.HTMLParser()
  url = "https://commitfest.postgresql.org/%s/" % (commitfest_id,)
  next_line_has_authors = False
  state = None
  for line in slow_fetch(url).splitlines():
    groups = re.search('\<a href="([0-9]+)/"\>([^<]+)</a>', line)
    if groups:
      submission_id = groups.group(1)
      name = parser.unescape(groups.group(2))
    if next_line_has_authors:
      next_line_has_authors = False
      groups = re.search("<td>([^<]*)</td>", line)
      if groups:
        authors = groups.group(1)
        authors = re.sub(" *\\([^)]*\\)", "", authors)
        result.append(Submission(submission_id, commitfest_id, name, state, authors))
        continue
    groups = re.search('<td><span class="label label-[^"]*">([^<]+)</span></td>', line)
    if groups:
      state = groups.group(1)
      next_line_has_authors = True
      continue
    next_line_has_authors = False
  return result

def get_current_commitfest_id():
  """Find the ID of the current open or next future commitfest."""
  result = None
  for line in slow_fetch("https://commitfest.postgresql.org").splitlines():
    groups = re.search('<a href="/([0-9]+)/">[0-9]+-[0-9]+</a> \((Open|In Progress) ', line)
    if groups:
      commitfest_id = groups.group(1)
      state = groups.group(2)
      result = int(commitfest_id)
  return result

def read_file(path):
  """Return the contents of file 'path'."""
  with open(path) as f:
    return f.read()

def write_file(path, data):
  """Write 'data' into 'path' atomically."""
  with open(path + ".tmp", "w+") as f:
    f.write(data)
  os.rename(path + ".tmp", path)

def sort_and_rotate_submissions(log, submissions):
  """Sort the given list of submissions, and then rotate them so that the one
     that follows 'last_submission_id' comes first (unless it is None).  This
     provides a simple way for us to carry on where we left off each time we
     run."""
  last_submission_id = load_last_submission_id(log)

  submissions = sorted(submissions, key=lambda s: s.id)
  if last_submission_id == None:
      return submissions
  done = [s for s in submissions if s.id <= last_submission_id]
  rest = [s for s in submissions if s.id > last_submission_id]
  return rest + done

LAST_SUBMISSION_ID_PATH = "last_submission_id"

def load_last_submission_id(log):
  """ what was the last submission ID we checked? """
  if os.path.exists(LAST_SUBMISSION_ID_PATH):
    last_submission_id = int(read_file(LAST_SUBMISSION_ID_PATH))
    log.write("last submission ID was %s\n" % last_submission_id)
    log.flush()
  else:
    last_submission_id = None

def write_last_submission_id(submission):
  """ update the last submission ID we checked? """
  write_file(LAST_SUBMISSION_ID_PATH, str(submission.id))

def check_n_submissions(log, commit_id, submissions, n):
  activity_message = "Idle."

  # now process n submissions, starting after that one
  for submission in sort_and_rotate_submissions(log, submissions):
    log.write("==> considering submission ID %s\n" % submission.id)
    log.flush()
    patch_dir = os.path.join("patches", str(submission.commitfest_id), str(submission.id))
    if os.path.isdir(patch_dir):
      # write name and status to disk so our web page builder can use them...
      write_file(os.path.join(patch_dir, "status"), submission.status)
      write_file(os.path.join(patch_dir, "name"), submission.name)
    thread_url = get_thread_url_for_submission(submission.commitfest_id, submission.id)
    #if submission.status not in ("Ready for Committer", "Needs review"):
    #  continue
    if thread_url == None:
      continue

    new_patch = False
    message_id, patches = get_latest_patches_from_thread_url(thread_url)
    if message_id:
      # download the patches, if we don't already have them
      message_id_path = os.path.join(patch_dir, "message_id")
      if not os.path.exists(message_id_path) or read_file(message_id_path) != message_id:
        new_patch = True # affects the friendly status message
        log.write("    message ID %s is new\n" % message_id)
        log.flush()
        tmp = patch_dir + ".tmp"
        if os.path.exists(tmp):
          shutil.rmtree(tmp)
        if os.path.exists(patch_dir):
          shutil.rmtree(patch_dir)
        os.mkdir(tmp)
        for patch in patches:
          parsed = urlparse.urlparse(patch)
          filename = os.path.basename(parsed.path)
          dest = os.path.join(tmp, filename)
          log.write("    fetching patch %s\n" % patch)
          log.flush()
          urllib.urlretrieve(patch, dest)
          time.sleep(SLOW_FETCH_SLEEP)
        write_file(os.path.join(tmp, "message_id"), message_id)
        write_file(os.path.join(tmp, "status"), submission.status)
        write_file(os.path.join(tmp, "name"), submission.name)
        os.rename(tmp, patch_dir)

      # if the commit ID has moved since last time, or we
      # have a new patchest, then we need to make a new branch
      # to trigger a new build
      commit_id_path = os.path.join("patches", str(submission.commitfest_id), str(submission.id), "commit_id")
      if not os.path.exists(commit_id_path) or read_file(commit_id_path) != commit_id:
        log.write("    commit ID %s is new\n" % commit_id)
        log.flush()
        branch = "commitfest/%s/%s" % (submission.commitfest_id, submission.id)
        subprocess.check_call("cd postgresql && git checkout . > /dev/null && git clean -fd > /dev/null && git checkout -q master", shell=True)
        failed_to_apply = False
        with open(os.path.join("logs", str(submission.commitfest_id), str(submission.id) + ".log"), "w") as apply_log:
          apply_log.write("== Fetched patches from message ID %s\n" % message_id)
          apply_log.write("== Applying on top of commit %s\n" % commit_id)
          for path in sorted(os.listdir(patch_dir)):
            if path.endswith(".patch"):
              with open(os.path.join(patch_dir, path), "r") as f:
                apply_log.write("== Applying patch %s...\n" % path)
                apply_log.flush()
                popen = subprocess.Popen("cd postgresql && patch -p1 --no-backup-if-mismatch --batch --silent", shell=True, stdin=f, stdout=apply_log, stderr=apply_log)
                popen.wait()
                if popen.returncode != 0:
                  failed_to_apply = True
                  break
            elif path.endswith(".patch.gz"):
              with gzip.open(os.path.join(patch_dir, path), "r") as f:
                apply_log.write("== Applying patch %s...\n" % path)
                apply_log.flush()
                popen = subprocess.Popen("cd postgresql && patch -p1 --no-backup-if-mismatch --batch --silent", shell=True, stdin=subprocess.PIPE, stdout=apply_log, stderr=apply_log)
                popen.communicate(input=f.read())
                popen.wait()
                if popen.returncode != 0:
                  failed_to_apply = True
                  break
            elif path.endswith(".tgz") or path.endswith(".tar.gz") or path.endswith(".tar.bz2"):
              apply_log.write("== Applying patches from tarball %s...\n" % path)
              apply_log.flush()
              # TODO catch errors manipulating tar files...
              with tarfile.open(os.path.join(patch_dir, path), "r") as tarball:
                for name in sorted(tarball.getnames()):
                  if not name.endswith(".patch") or "/." in name:
                    continue
                  apply_log.write("== Applying patch %s...\n" % name)
                  apply_log.flush()
                  popen = subprocess.Popen("cd postgresql && patch -p1 --no-backup-if-mismatch --batch --silent", shell=True, stdin=subprocess.PIPE, stdout=apply_log, stderr=apply_log)
                  f = tarball.extractfile(name)
                  popen.communicate(input=f.read())
                  f.close()
                  popen.wait()
                  if popen.returncode != 0:
                    failed_to_apply = True
                    break
        apply_status_path = os.path.join("patches", str(submission.commitfest_id), str(submission.id), "apply_status")
        if failed_to_apply:
          log.write("    apply failed (see apply log for details)\n")
          log.flush()
          write_file(apply_status_path, "failing")
          # no point in trying again until either the message ID or the commit ID moves
          write_file(commit_id_path, commit_id)
        else:
          write_file(apply_status_path, "passing")
          write_file("postgresql/.travis.yml", TRAVIS_FILE)
          subprocess.call("cd postgresql && git branch -q -D %s > /dev/null 2> /dev/null" % (branch,), shell=True) # ignore if fail
          subprocess.check_call("cd postgresql && git checkout -q -b %s" % (branch,), shell=True)
          subprocess.check_call("cd postgresql && git add -A", shell=True)
          log.write("    creating new branch %s\n" % branch)
          log.flush()
          write_file("commit_message", """[CF %s/%s] %s

This commit was automatically generated by cfbot at commitfest.cputube.org.
It is based on patches submitted to the PostgreSQL mailing lists and
registered in the PostgreSQL Commitfest application.

This branch will be overwritten each time a new patch version is posted to
the email thread or the master branch changes.

Commitfest entry: https://commitfest.postgresql.org/%s/%s
Patch(es): https://www.postgresql.org/message-id/%s
Author(s): %s
""" % (submission.commitfest_id, submission.id, submission.name, submission.commitfest_id, submission.id, message_id, submission.authors))
          subprocess.check_call("cd postgresql && git commit -q -F ../commit_message", shell=True)
          write_file(commit_id_path, commit_id)
          if False: # disable pushing for my own testing purposes
            log.write("    pushing branch %s\n" % branch)
            log.flush()
            os.environ["GIT_SSH_COMMAND"] = CFBOT_REPO_SSH_COMMAND
            subprocess.check_call("cd postgresql && git push -q -f cfbot-repo %s" % (branch,), shell=True)
            if new_patch:
              activity_message = """Pushed branch <a href="https://github.com/postgresql-cfbot/postgresql/tree/%s">%s</a>, triggered by <a href="https://www.postgresql.org/message-id/%s">new patch</a>.""" % (branch, branch, message_id)
            else:
              activity_message = """Pushed branch <a href="https://github.com/postgresql-cfbot/postgresql/tree/%s">%s</a>, triggered by commit <a href="https://git.postgresql.org/gitweb/?p=postgresql.git;a=commitdiff;h=%s">%s</a>.  Waiting for a while to be polite before rebuilding items marked "&bull;"...""" % (branch, branch, commit_id, commit_id[:8])
          n = n - 1

      # remember this ID so we can start after this next time
      write_last_submission_id(submission)

      if n <= 0:
        break
  return activity_message

def make_author_url(author):
    text = author.strip()
    text = unicode(text, "utf-8")
    text = unicodedata.normalize('NFD', text)
    text = text.encode('ascii', 'ignore')
    text = text.decode("utf-8")
    text = str(text).lower()
    text = re.sub('[ ]+', '-', text)
    text = re.sub('[^0-9a-zA-Z_-]', '', text)
    return text + ".html"


def slow_fetch(url):
  """Fetch the body of a web URL, but sleep every time too to be kind to the
     commitfest server."""
  opener = urllib2.build_opener()
  opener.addheaders = [('User-Agent', USER_AGENT)]
  response = opener.open(url)
  body = response.read()
  response.close()
  time.sleep(SLOW_FETCH_SLEEP)
  return body
  
def get_latest_patches_from_thread_url(thread_url):
  """Given a 'whole thread' URL from the archives, find the last message that
     had at least one attachment called something.patch.  Return the message
     ID and the list of URLs to fetch all the patches."""
  selected_message_attachments = []
  selected_message_id = None
  message_attachments = []
  message_id = None
  for line in slow_fetch(thread_url).splitlines():
    groups = re.search('<a href="(/message-id/attachment/[^"]*\\.(patch|patch\\.gz|tar\\.gz|tgz|tar\\.bz2))">', line)
    if groups:
      message_attachments.append("https://www.postgresql.org" + groups.group(1))
      selected_message_attachments = message_attachments
      selected_message_id = message_id
    else:
      groups = re.search('<a name="([^"]+)"></a>', line)
      if groups:
        message_id = groups.group(1)
        message_attachments = []
  # if there is a tarball attachment, there must be only one attachment,
  # otherwise give up on this thread (we don't know how to combine patches and
  # tarballs)
  if selected_message_attachments != None:
    if any(x.endswith(".tgz") or x.endswith(".tar.gz") or x.endswith(".tar.bz2") for x in selected_message_attachments):
      if len(selected_message_attachments) > 1:
        selected_message_id = None
        selected_message_attachments = None
  # if there are multiple patch files, they had better follow the convention
  # of leading numbers, otherwise we don't know how to apply them in the right
  # order
  # TODO
  return selected_message_id, selected_message_attachments


 
def build_web_page(commit_id, commitfest_id, submissions, filter_author, activity_message, path):
  """Build a web page that lists all known entries and shows the badges."""

  last_status = None
  submissions = sorted(submissions, key=lambda s: s.sort_status_name)
  commitfest_id_for_link = commitfest_id
  if commitfest_id_for_link == None:
    commitfest_id_for_link = ""
  with open(path + ".tmp", "w") as f:
    f.write("""
<html>
  <head>
    <meta charset="UTF-8"/>
    <title>PostgreSQL Patch Tester</title>
    <style type="text/css">
      body {
        margin: 1rem auto;
        font-family: -apple-system,BlinkMacSystemFont,avenir next,avenir,helvetica neue,helvetica,ubuntu,roboto,noto,segoe ui,arial,sans-serif;
        color: #444;
        max-width: 920px;
      }
      h1 {
        font-size: 3rem;
      }
      h2 {
        font-size: 2rem;
      }
      table {
        border-collapse: collapse;
      	font-size: 0.875rem;
        width: 100%%;
      }
      td {
        padding: 1rem 1rem 1rem 0;
        border-bottom: solid 1px rgba(0,0,0,.2);
      }
    </style>
  </head>
  <body>
    <h1>PostgreSQL Patch Tester</h1>
    <p>
      Here lives an experimental bot that does this:
      <a href="https://commitfest.postgresql.org/%s">Commitfest</a>
      &rarr; 
      <a href="https://github.com/postgresql-cfbot/postgresql/branches">Github</a>
      &rarr;
      <a href="https://travis-ci.org/postgresql-cfbot/postgresql/branches">Travis CI</a>
      &rarr;
      <a href="https://codecov.io/gh/postgresql-cfbot/postgresql/commits">Codecov</a>.
      You can find a report for the <a href="index.html">current CF</a>, the
      <a href="next.html">next CF</a> or individual patch authors.
    </p>
    <p>Current status: %s</p>
    <table>
""" % (commitfest_id_for_link, activity_message))
    for submission in sorted(submissions, key=lambda s: s.sort_status_name):

      # skip if we need to filter by commitfest
      if commitfest_id != None and submission.commitfest_id != commitfest_id:
        continue

      # skip if we need to filter by author
      if filter_author != None and filter_author not in submission.all_authors():
        continue

      # load the info about this submission that was recorded last time
      # we actually rebuilt the branch
      submission_dir = os.path.join("patches", str(submission.commitfest_id), str(submission.id))
      apply_status_path = os.path.join(submission_dir, "apply_status")
      message_id_path = os.path.join(submission_dir, "message_id")
      commit_id_path = os.path.join(submission_dir, "commit_id")
      name_path = os.path.join(submission_dir, "name")
      status_path = os.path.join(submission_dir, "status")
      if not os.path.exists(apply_status_path) or not os.path.exists(message_id_path) or not os.path.exists(name_path) or not os.path.exists(status_path):
        continue
      apply_status = read_file(apply_status_path)
      message_id = read_file(message_id_path)
      name = submission.name #read_file(name_path)
      status = submission.status #read_file(status_path)

      # check if this submission is queued for rebuilding
      build_needed_indicator = False
      if apply_status == "passing" and (not os.path.exists(commit_id_path) or read_file(commit_id_path) != commit_id):
        build_needed_indicator = True

      # create a new heading row if this is a new CF status
      if last_status == None or last_status != status:
        f.write("""      <tr><td colspan="6"><h2>%s</h2></td></tr>\n""" % status)
        last_status = status

      # create an apply pass/fail badge
      commitfest_dir = os.path.join("www", str(submission.commitfest_id))
      if not os.path.exists(commitfest_dir):
        os.mkdir(commitfest_dir)
      # write an image file for each submission, so that the badge could be included on other websites
      if apply_status == "failing":
        write_file(os.path.join(commitfest_dir, "%s.apply.svg" % (submission.id,)), APPLY_FAILING_SVG)
      else:
        write_file(os.path.join(commitfest_dir, "%s.apply.svg" % (submission.id,)), APPLY_PASSING_SVG)
      write_file(os.path.join(commitfest_dir, "%s.log" % submission.id), read_file(os.path.join("logs", str(submission.commitfest_id), str(submission.id) + ".log")))
      if len(name) > 80:
        name = name[:80] + "..."
      # convert list of authors into links
      author_links = []
      for author in submission.all_authors():
        author_links.append("""<a href="%s">%s</a>""" % (make_author_url(author), author))
      author_links_string = ", ".join(author_links)
      # write out an entry
      f.write("""
      <tr>
        <td>%s/%s</td>
        <td><a href="https://commitfest.postgresql.org/%s/%s/">%s</a></td>
        <td>%s</td>
        <td><a href="https://www.postgresql.org/message-id/%s">patch</a></td>
""" % (submission.commitfest_id, submission.id, submission.commitfest_id, submission.id, name, author_links_string, message_id))
      if apply_status == "failing":
        f.write("""        <td><a href="%s/%s.log"><img src="apply-failing.svg"/></a></td>\n""" % (submission.commitfest_id, submission.id))
        f.write("""        <td></td>\n""")
      else:
        f.write("""        <td><a href="%s/%s.log"><img src="apply-passing.svg"/></a></td>\n""" % (submission.commitfest_id, submission.id))
        #f.write("""        <td><a href="https://github.com/postgresql-cfbot/postgresql/tree/commitfest/%s/%s"><img src="apply-passing.svg"/></a></td>\n""" % (commitfest_id, submission.id))
        f.write("""        <td><a href="https://travis-ci.org/postgresql-cfbot/postgresql/branches"><img src="https://travis-ci.org/postgresql-cfbot/postgresql.svg?branch=commitfest/%s/%s" alt="Build Status" /></a></td>\n""" % (submission.commitfest_id, submission.id))
        if build_needed_indicator:
          f.write("""        <td>&bull;</td>\n""")
        else:
          f.write("""        <td></td>\n""")
      f.write("      </tr>\n")
    f.write("""
    </table>

    <p>Please send feedback to thomas.munro-at-enterprisedb.com.</p>
  </body>
</html>
""")
  os.rename(path + ".tmp", path)

def unique_authors(submissions):
  results = []
  for submission in submissions:
    results += submission.all_authors()
  return list(set(results))
