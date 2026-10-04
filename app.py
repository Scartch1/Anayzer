# app.py - Standalone Code Analyzer Backend (No Firebase)
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import os
from dotenv import load_dotenv
import tempfile
import shutil
import subprocess
import threading
import re
import uuid
from git import Repo
import socket
import sys
import requests
from language_support import (
    SUPPORTED_EXTENSIONS,
    SUPPORTED_LANGUAGES,
    analyze_syntax,
    compile_source,
    detect_language,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'code-analyzer-secret-key')

# Enable CORS for all routes
CORS(app, origins=[
    'http://localhost:5500',
    'http://127.0.0.1:5500',
    'http://localhost:3000',
    'http://127.0.0.1:3000',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'https://analyzec1-production.up.railway.app',
    'https://cd-tracker-nine.vercel.app'
], supports_credentials=True, allow_headers=['Content-Type', 'Authorization'])

# In-memory storage for analysis results (since no Firebase)
analysis_storage = {}

def analyze_file(file_path, language, repo_root=None):
    """Analyze a single source file."""
    errors, warnings, _ = analyze_syntax(file_path, language, repo_root=repo_root)
    return errors, warnings

def detect_branch(repo_url):
    """Detect the default branch of a GitHub repository"""
    try:
        parts = repo_url.rstrip('/').split('/')
        if len(parts) >= 5:
            user = parts[-2]
            repo_name = parts[-1].replace('.git', '')
            
            api_url = f"https://api.github.com/repos/{user}/{repo_name}"
            headers = {}
            if os.getenv('GITHUB_TOKEN'):
                headers['Authorization'] = f"token {os.getenv('GITHUB_TOKEN')}"
            
            response = requests.get(api_url, headers=headers)
            if response.status_code == 200:
                repo_info = response.json()
                return repo_info.get('default_branch', 'main')
    except Exception as e:
        print(f"⚠️ Could not detect default branch: {e}")
    
    return 'main'

def analyze_repository_background(analysis_id, repo_url, branch):
    """Background task for repository analysis"""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        print(f"📦 Cloning {repo_url} to {temp_dir}")
        
        # Update status to cloning
        analysis_storage[analysis_id]['status'] = 'cloning'
        
        # Try multiple branches
        branches_to_try = [branch, 'main', 'master']
        cloned_successfully = False
        used_branch = None
        
        for try_branch in branches_to_try:
            try:
                print(f"   Trying branch: {try_branch}")
                repo = Repo.clone_from(repo_url, temp_dir, branch=try_branch, depth=1)
                used_branch = try_branch
                cloned_successfully = True
                print(f"   ✅ Cloned using branch: {try_branch}")
                break
            except Exception as clone_error:
                print(f"   ❌ Failed with branch {try_branch}: {clone_error}")
                if temp_dir and os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    temp_dir = tempfile.mkdtemp()
                continue
        
        if not cloned_successfully:
            raise Exception(f"Failed to clone repository. Tried branches: {branches_to_try}")
        
        # Update status to analyzing
        analysis_storage[analysis_id]['status'] = 'analyzing'
        analysis_storage[analysis_id]['branch_used'] = used_branch
        
        # Find supported source files
        source_files = []
        
        for root, dirs, files in os.walk(temp_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.git' and d != '__pycache__']
            
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, temp_dir)

                language = detect_language(file)
                if language:
                    source_files.append((file_path, rel_path, language, file))
        
        print(f"🔍 Found {len(source_files)} supported source files")
        
        if len(source_files) == 0:
            analysis_storage[analysis_id]['status'] = 'completed'
            analysis_storage[analysis_id]['summary'] = {
                'total_files': 0,
                'errors_count': 0,
                'warnings_count': 0,
                'branch_used': used_branch
            }
            analysis_storage[analysis_id]['files'] = []
            return
        
        total_errors = 0
        total_warnings = 0
        analyzed_files = []
        
        for file_path, rel_path, language, file_name in source_files:
            try:
                # Read file content
                content = ""
                try:
                    encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1', 'ascii']
                    for encoding in encodings:
                        try:
                            with open(file_path, 'r', encoding=encoding) as f:
                                content = f.read()
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        with open(file_path, 'rb') as f:
                            content = f.read().decode('utf-8', errors='ignore')
                except Exception as e:
                    content = f"// Error reading file: {str(e)}"
                
                # Analyze file
                errors, warnings = analyze_file(file_path, language, repo_root=temp_dir)
                
                file_result = {
                    'file_path': rel_path.replace('\\', '/'),
                    'file_name': file_name,
                    'language': language,
                    'code': content,
                    'errors': errors,
                    'warnings': warnings,
                    'errors_count': len(errors),
                    'warnings_count': len(warnings)
                }
                
                analyzed_files.append(file_result)
                
                total_errors += len(errors)
                total_warnings += len(warnings)
                
                if errors:
                    status_icon = "❌"
                elif warnings:
                    status_icon = "⚠️"
                else:
                    status_icon = "✅"
                    
                print(f"{status_icon} {rel_path} (Errors: {len(errors)}, Warnings: {len(warnings)})")
                
            except Exception as e:
                print(f"❌ Error processing {rel_path}: {e}")
                file_result = {
                    'file_path': rel_path.replace('\\', '/'),
                    'file_name': file_name,
                    'language': language,
                    'code': f"// Error processing file: {str(e)}",
                    'errors': [{'line': 0, 'message': f'Processing error: {str(e)}', 'type': 'error'}],
                    'warnings': [],
                    'errors_count': 1,
                    'warnings_count': 0
                }
                analyzed_files.append(file_result)
                total_errors += 1
        
        # Store results
        analysis_storage[analysis_id]['status'] = 'completed'
        analysis_storage[analysis_id]['summary'] = {
            'total_files': len(source_files),
            'errors_count': total_errors,
            'warnings_count': total_warnings,
            'branch_used': used_branch
        }
        analysis_storage[analysis_id]['files'] = analyzed_files
        
        print(f"\n✅ Analysis complete for {analysis_id}")
        print(f"   Total files: {len(source_files)}")
        print(f"   Total errors: {total_errors}")
        print(f"   Total warnings: {total_warnings}")
        
    except Exception as e:
        print(f"❌ Background analysis failed: {e}")
        analysis_storage[analysis_id]['status'] = 'error'
        analysis_storage[analysis_id]['error'] = str(e)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"🧹 Cleaned up temporary directory")

@app.route('/api/analyze', methods=['POST'])
def analyze_repository():
    """Analyze a GitHub repository"""
    try:
        data = request.json
        repo_url = data.get('repo_url')
        
        if not repo_url:
            return jsonify({'success': False, 'error': 'Repository URL required'}), 400
        
        # Generate unique analysis ID
        analysis_id = str(uuid.uuid4())
        
        # Detect branch
        branch = detect_branch(repo_url)
        
        # Initialize storage
        analysis_storage[analysis_id] = {
            'id': analysis_id,
            'repo_url': repo_url,
            'branch': branch,
            'status': 'pending',
            'created_at': datetime.utcnow().isoformat(),
            'summary': None,
            'files': []
        }
        
        # Start analysis in background
        thread = threading.Thread(
            target=analyze_repository_background,
            args=(analysis_id, repo_url, branch)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'analysis_id': analysis_id,
            'branch_used': branch,
            'message': 'Analysis started'
        })
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/analysis/<analysis_id>', methods=['GET'])
def get_analysis(analysis_id):
    """Get analysis results"""
    try:
        if analysis_id not in analysis_storage:
            return jsonify({'success': False, 'error': 'Analysis not found'}), 404
        
        analysis = analysis_storage[analysis_id]
        
        response = {
            'success': True,
            'analysis_id': analysis_id,
            'status': analysis['status'],
            'repo_url': analysis['repo_url']
        }
        
        if analysis['status'] == 'completed':
            response['summary'] = analysis['summary']
            response['files'] = analysis['files']
        elif analysis['status'] == 'error':
            response['error'] = analysis.get('error', 'Unknown error')
        
        return jsonify(response)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/compile', methods=['POST'])
def compile_code():
    """Automatically detect and compile/check Java, Python, C++, or C code.

    Submitted code is never executed; only the language syntax/compiler check
    is run inside a temporary directory.
    """
    try:
        data = request.get_json(silent=True) or {}
        source_code = data.get('code', data.get('source_code'))
        language = data.get('language')
        file_name = data.get('file_name', data.get('filename'))

        if source_code is None:
            return jsonify({
                'success': False,
                'error': 'Source code is required in the "code" field.'
            }), 400
        if not isinstance(source_code, str):
            return jsonify({'success': False, 'error': 'Source code must be a string.'}), 400
        if file_name is not None and not isinstance(file_name, str):
            return jsonify({'success': False, 'error': 'Filename must be a string.'}), 400
        if len(source_code.encode('utf-8')) > 1_000_000:
            return jsonify({'success': False, 'error': 'Source code must be 1 MB or smaller.'}), 413

        result = compile_source(source_code, language=language, file_name=file_name)
        return jsonify({'success': True, **result})
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        print(f'Compile error: {exc}')
        return jsonify({'success': False, 'error': 'Unable to compile source code.'}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'service': 'Code Analyzer API'
    })

@app.route('/', methods=['GET'])
def home():
    return jsonify({
        'name': 'Code Analyzer API',
        'version': '1.0.0',
        'status': 'running',
        'endpoints': [
            'POST /api/analyze - Analyze a GitHub repository',
            'GET /api/analysis/<analysis_id> - Get analysis results',
            'POST /api/compile - Automatically compile/check a source snippet',
            'GET /api/health - Health check'
        ],
        'supported_languages': SUPPORTED_LANGUAGES,
        'supported_extensions': SUPPORTED_EXTENSIONS
    })

def find_free_port():
    """Find a free port on the system"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

# Cleanup old analyses periodically (optional)
def cleanup_old_analyses():
    """Remove analyses older than 1 hour"""
    now = datetime.utcnow()
    to_delete = []
    for analysis_id, analysis in analysis_storage.items():
        created_at = datetime.fromisoformat(analysis['created_at'])
        if (now - created_at).total_seconds() > 3600:  # 1 hour
            to_delete.append(analysis_id)
    
    for analysis_id in to_delete:
        del analysis_storage[analysis_id]
    
    if to_delete:
        print(f"🧹 Cleaned up {len(to_delete)} old analyses")

if __name__ == '__main__':
    # Get port from environment variable (Railway sets this)
    port = int(os.getenv('PORT', 5000))
    
    # For local development
    if not os.getenv('RAILWAY_ENVIRONMENT'):
        # Find available port for local development
        ports_to_try = [5000, 5001, 8080, 3000, 8000, 8888]
        selected_port = None
        
        print("🔍 Checking available ports...")
        for test_port in ports_to_try:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('127.0.0.1', test_port))
                    selected_port = test_port
                    print(f"   ✓ Port {test_port} is available")
                    break
            except OSError:
                print(f"   ✗ Port {test_port} is in use")
                continue
        
        if selected_port is None:
            selected_port = find_free_port()
            print(f"   ℹ️ Using automatically assigned port: {selected_port}")
        
        print("=" * 60)
        print("🚀 Code Analyzer Backend Server")
        print("=" * 60)
        print(f"🌐 Server running on: http://127.0.0.1:{selected_port}")
        print(f"📡 API Endpoints:")
        print(f"   POST /api/analyze - Analyze repository")
        print(f"   GET /api/analysis/<id> - Get results")
        print(f"   GET /api/health - Health check")
        print("=" * 60)
        
        app.run(debug=True, port=selected_port, host='127.0.0.1')
    else:
        # Production on Railway
        print("=" * 60)
        print("🚀 Code Analyzer Backend Server (Production)")
        print("=" * 60)
        print(f"🌐 Server running on port: {port}")
        print(f"📡 API Endpoints:")
        print(f"   POST /api/analyze - Analyze repository")
        print(f"   GET /api/analysis/<id> - Get results")
        print(f"   GET /api/health - Health check")
        print("=" * 60)
        
        app.run(debug=False, port=port, host='0.0.0.0')
