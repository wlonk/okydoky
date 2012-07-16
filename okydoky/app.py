""":mod:`okydoky.app` --- Web hook
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
import base64
import functools
import logging
import os
import os.path
import re
import shutil
import subprocess
import sys
import tarfile

from eventlet import spawn_n
from eventlet.green import urllib2
from eventlet.greenpool import GreenPool
from flask import (Flask, abort, current_app, json, make_response, redirect,
                   request, render_template, session, url_for)
from flask.helpers import send_from_directory
from virtualenv import create_environment
from werkzeug.urls import url_decode, url_encode


REQUIRED_CONFIGS = ('REPOSITORY', 'CLIENT_ID', 'CLIENT_SECRET',
                    'SAVE_DIRECTORY', 'SECRET_KEY')

app = Flask(__name__)


def open_file(filename, mode='r', config=None):
    config = config or current_app.config
    save_path = config['SAVE_DIRECTORY']
    if not os.path.isdir(save_path):
        os.makedirs(save_path)
    return open(os.path.join(save_path, filename), mode)


def open_token_file(mode='r', config=None):
    return open_file('token.txt', mode, config=config)


def get_token(config=None):
    config = config or current_app.config
    try:
        token = config['ACCESS_TOKEN']
    except KeyError:
        try:
            with open_token_file(config=config) as f:
                token = f.read().strip()
        except IOError:
            return None
        config['ACCESS_TOKEN'] = token
    return token


def open_head_file(mode='r', config=None):
    return open_file('head.txt', mode, config=config)


def get_head(config=None):
    try:
        with open_head_file(config=config) as f:
            return f.read().strip()
    except IOError:
        pass


@app.route('/')
def home():
    token = get_token()
    if token is None:
        return render_template('home.html', login_url=url_for('auth_redirect'))
    head = get_head()
    if head is None:
        hook_url = url_for('post_receive_hook', _external=True)
        return render_template('empty.html', hook_url=hook_url)
    return redirect(url_for('docs', ref=head))


@app.route('/<ref>/', defaults={'path': 'index.html'})
@app.route('/<ref>/<path:path>')
def docs(ref, path):
    if not re.match(r'^[A-Fa-f0-9]{7,40}$', ref):
        abort(404)
    save_dir = current_app.config['SAVE_DIRECTORY']
    if not session.get('login'):
        back = base64.urlsafe_b64encode(request.url)
        params = {
            'client_id': current_app.config['CLIENT_ID'],
            'redirect_uri': url_for('auth', back=back, _external=True),
            'scope': ''
        }
        return redirect('https://github.com/login/oauth/authorize?' +
                        url_encode(params))
    if len(ref) < 40:
        for candi in os.listdir(save_dir):
            if (os.path.isdir(os.path.join(save_dir, candi)) and
                candi.startswith(ref)):
                return redirect(url_for('docs', ref=candi, path=path))
        abort(404)
    return send_from_directory(save_dir, os.path.join(ref, path))


@app.route('/auth')
def auth_redirect():
    params = {
        'client_id': current_app.config['CLIENT_ID'],
        'redirect_uri': url_for('auth', _external=True),
        'scope': 'repo'
    }
    return redirect('https://github.com/login/oauth/authorize?' +
                    url_encode(params))


@app.route('/auth/finalize')
def auth():
    try:
        back = request.args['back']
    except KeyError:
        redirect_uri = url_for('auth', _external=True)
        initial = True
    else:
        redirect_uri = url_for('auth', back=back, _external=True)
        initial = False
    params = {
        'client_id': current_app.config['CLIENT_ID'],
        'client_secret': current_app.config['CLIENT_SECRET'],
        'redirect_uri': redirect_uri,
        'code': request.args['code'],
        'state': request.args['state']
    }
    response = urllib2.urlopen(
        'https://github.com/login/oauth/access_token',
        url_encode(params)
    )
    auth_data = url_decode(response.read())
    response.close()
    token = auth_data['access_token']
    if initial:
        with open_token_file('w') as f:
            f.write(token)
        current_app.config['ACCESS_TOKEN'] = token
        return_url = url_for('home')
    else:
        return_url = base64.urlsafe_b64decode(str(back))
    session['login'] = token
    return redirect(return_url)


@app.route('/', methods=['POST'])
def post_receive_hook():
    payload = json.loads(request.form['payload'])
    commits = [commit['id'] for commit in payload['commits']]
    spawn_n(build_main, commits, dict(current_app.config))
    response = make_response('true', 202)
    response.mimetype = 'application/json'
    return response


def build_main(commits, config):
    logger = logging.getLogger(__name__ + '.build_main')
    logger.info('triggered with %d commits', len(commits))
    logger.debug('commits = %r', commits)
    token = get_token(config)
    pool = GreenPool()
    results = pool.imap(
        functools.partial(download_archive, token=token, config=config),
        commits
    )
    env = make_virtualenv(config)
    save_dir = config['SAVE_DIRECTORY']
    for commit, filename in results:
        working_dir = extract(filename, save_dir)
        build = build_sphinx(working_dir, env)
        result_dir = os.path.join(save_dir, commit)
        shutil.move(build, result_dir)
        logger.info('build complete: %s' % result_dir)
        shutil.rmtree(working_dir)
        logger.info('working directory %s has removed' % working_dir)
    with open_head_file('w', config=config) as f:
        f.write(commits[0])
    logger.info('new head: %s', commits[0])


def download_archive(commit, token, config):
    logger = logging.getLogger(__name__ + '.download_archive')
    logger.info('start downloading archive %s', commit)
    url_p = 'https://api.github.com/repos/{0}/tarball/{1}?access_token={2}'
    url = url_p.format(config['REPOSITORY'], commit, token)
    while 1:
        response = urllib2.urlopen(url)
        try:
            url = response.info()['Location']
        except KeyError:
            break
    filename = os.path.join(config['SAVE_DIRECTORY'], commit + '.tar.gz')
    logger.debug('save %s into %s', commit, filename)
    logger.debug('filesize of %s: %s',
                 filename, response.info()['Content-Length'])
    with open(filename, 'wb') as f:
        while 1:
            chunk = response.read(4096)
            if chunk:
                f.write(chunk)
                continue
            break
    logger.info('finish downloading archive %s: %s', commit, filename)
    return commit, filename


def extract(filename, path):
    logger = logging.getLogger(__name__ + '.extract')
    logger.info('extracting %s...', filename)
    tar = tarfile.open(filename)
    logger.debug('tar.getnames() = %r', tar.getnames())
    dirname = tar.getnames()[0]
    tar.extractall(path)
    result_path = os.path.join(path, dirname)
    logger.info('%s has extracted to %s', filename, result_path)
    os.unlink(filename)
    logger.info('%s has removed', filename)
    return result_path


def build_sphinx(path, env):
    logger = logging.getLogger(__name__ + '.build_sphinx')
    def run(cmd, **kwargs):
        logger.debug(' '.join(map(repr, cmd)))
        subprocess.call(cmd, **kwargs)
    if sys.platform == 'win32':
        bindir = os.path.join(env, 'Scripts')
    else:
        bindir = os.path.join(env, 'bin')
    python = os.path.join(bindir, 'python')
    logger.info('installing dependencies...')
    run([python, 'setup.py', 'develop'], cwd=path)
    logger.info('installing Sphinx...')
    run([os.path.join(bindir, 'easy_install'), 'Sphinx'])
    logger.info('building documentation using Sphinx...')
    run([python, 'setup.py', 'build_sphinx'], cwd=path)
    run([python, 'setup.py', 'develop', '--uninstall'], cwd=path)
    build = os.path.join(path, 'build', 'sphinx', 'html')
    logger.info('documentation: %s' % build)
    return build


def make_virtualenv(config):
    logger = logging.getLogger(__name__ + '.make_virtualenv')
    save_dir = config['SAVE_DIRECTORY']
    envdir = os.path.join(save_dir, '_env')
    if os.path.isdir(envdir):
        logger.info('virtualenv already exists: %s; skip...' % envdir)
        return envdir
    logger.info('creating new virtualenv: %s' % envdir)
    create_environment(envdir, use_distribute=True)
    logger.info('created virtualenv: %s' % envdir)
    return envdir