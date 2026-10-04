import ast
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as xml_escape


LANGUAGE_SPECS = {
    'c': {
        'label': 'C',
        'extensions': ['.c'],
    },
    'cpp': {
        'label': 'C++',
        'extensions': ['.cpp', '.cc', '.cxx', '.c++', '.h', '.hpp'],
    },
    'java': {
        'label': 'Java',
        'extensions': ['.java'],
    },
    'javascript': {
        'label': 'JavaScript',
        'extensions': ['.js', '.mjs', '.cjs'],
    },
    'csharp': {
        'label': 'C#',
        'extensions': ['.cs'],
    },
    'python': {
        'label': 'Python',
        'extensions': ['.py'],
    },
}


def _build_extension_map():
    mapping = {}
    for language, spec in LANGUAGE_SPECS.items():
        for extension in spec['extensions']:
            mapping[extension] = language
    return mapping


EXTENSION_TO_LANGUAGE = _build_extension_map()
SUPPORTED_LANGUAGES = [spec['label'] for spec in LANGUAGE_SPECS.values()]
SUPPORTED_EXTENSIONS = sorted({ext for spec in LANGUAGE_SPECS.values() for ext in spec['extensions']})

# Names accepted by the compile API.  The UI can use either the internal key,
# the display label, or the names commonly used in editor language pickers.
LANGUAGE_ALIASES = {
    'c': 'c',
    'clanguage': 'c',
    'c language': 'c',
    'cpp': 'cpp',
    'c++': 'cpp',
    'cplusplus': 'cpp',
    'c plus plus': 'cpp',
    'java': 'java',
    'python': 'python',
    'py': 'python',
}


def detect_language(file_name):
    """Return the internal language key for a filename, or None if unsupported."""
    extension = os.path.splitext(file_name)[1].lower()
    return EXTENSION_TO_LANGUAGE.get(extension)


def normalize_language(language):
    """Convert a client-supplied language name to a supported internal key."""
    if not language:
        return None
    normalized = re.sub(r'[_-]+', ' ', str(language).strip().lower())
    normalized = re.sub(r'\s+', ' ', normalized)
    return LANGUAGE_ALIASES.get(normalized) or (normalized if normalized in LANGUAGE_SPECS else None)


def language_label(language):
    """Return the user-facing label for an internal language key."""
    return LANGUAGE_SPECS[language]['label']


def get_missing_tool_hint(language):
    if language == 'c':
        return 'gcc'
    if language == 'cpp':
        return 'g++'
    if language == 'java':
        return 'javac'
    if language == 'javascript':
        return 'node'
    if language == 'csharp':
        return 'dotnet'
    if language == 'python':
        return 'python'
    return 'a supported compiler'


def build_analysis_command(language, file_path, repo_root=None):
    """Build the syntax-check command for a supported language."""
    cleanup_paths = []

    if language == 'c':
        return ['gcc', '-fsyntax-only', '-Wall', '-Wextra', '-std=c11', file_path], cleanup_paths

    if language == 'cpp':
        return ['g++', '-fsyntax-only', '-Wall', '-Wextra', '-std=c++14', file_path], cleanup_paths

    if language == 'java':
        output_dir = tempfile.mkdtemp(prefix='java-syntax-')
        cleanup_paths.append(output_dir)
        source_root = repo_root or os.path.dirname(file_path)
        return [
            'javac',
            '-proc:none',
            '-Xlint:all',
            '-d', output_dir,
            '-sourcepath', source_root,
            file_path
        ], cleanup_paths

    if language == 'javascript':
        return ['node', '--check', file_path], cleanup_paths

    if language == 'csharp':
        project_dir = tempfile.mkdtemp(prefix='csharp-syntax-')
        cleanup_paths.append(project_dir)
        project_path = os.path.join(project_dir, 'SyntaxCheck.csproj')
        project_root = os.path.abspath(project_dir).replace('\\', '/')
        compile_files = [os.path.abspath(file_path).replace('\\', '/')]

        if repo_root and os.path.isdir(repo_root):
            compile_files = []
            for root, dirs, files in os.walk(repo_root):
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.') and d not in ['bin', 'obj', 'build', 'dist', 'node_modules']
                ]
                for name in files:
                    if name.lower().endswith('.cs'):
                        compile_files.append(os.path.abspath(os.path.join(root, name)).replace('\\', '/'))

            if not compile_files:
                compile_files = [os.path.abspath(file_path).replace('\\', '/')]

        compile_items_lines = []
        for path in compile_files:
            escaped_path = xml_escape(path).replace('"', '&quot;')
            compile_items_lines.append(f'    <Compile Include="{escaped_path}" />')
        compile_items = '\n'.join(compile_items_lines)
        project_xml = f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <OutputType>Library</OutputType>
    <LangVersion>latest</LangVersion>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <BaseOutputPath>{project_root}/bin/</BaseOutputPath>
    <BaseIntermediateOutputPath>{project_root}/obj/</BaseIntermediateOutputPath>
  </PropertyGroup>
  <ItemGroup>
{compile_items}
  </ItemGroup>
</Project>
'''
        with open(project_path, 'w', encoding='utf-8') as project_file:
            project_file.write(project_xml)
        return ['dotnet', 'build', project_path, '--nologo', '-clp:NoSummary'], cleanup_paths

    raise ValueError(f'Unsupported language: {language}')


_STANDARD_DIAGNOSTIC_RE = re.compile(
    r'^(.*?):(\d+)(?::(\d+))?:\s+(fatal error|error|warning):\s+(.*)$',
    re.IGNORECASE
)
_CSHARP_DIAGNOSTIC_RE = re.compile(
    r'^(.*)\((\d+)(?:,(\d+))?\):\s+(error|warning)\s+([A-Z]+\d+):\s+(.*)$',
    re.IGNORECASE
)


def _add_diagnostic(errors, warnings, kind, line, message):
    entry = {
        'line': int(line) if line else 0,
        'message': message.strip(),
        'type': kind,
    }
    if kind == 'warning':
        warnings.append(entry)
    else:
        errors.append(entry)


def _parse_standard_output(output):
    errors = []
    warnings = []
    seen = set()

    for line in output.splitlines():
        match = _STANDARD_DIAGNOSTIC_RE.match(line.strip())
        if not match:
            continue

        line_num = int(match.group(2))
        kind = 'warning' if match.group(4).lower() == 'warning' else 'error'
        message = match.group(5).strip()
        key = (line_num, kind, message)
        if key in seen:
            continue
        seen.add(key)
        _add_diagnostic(errors, warnings, kind, line_num, message)

    return errors, warnings


def _parse_csharp_output(output):
    errors = []
    warnings = []
    seen = set()

    for line in output.splitlines():
        match = _CSHARP_DIAGNOSTIC_RE.match(line.strip())
        if not match:
            continue

        line_num = int(match.group(2))
        kind = match.group(4).lower()
        code = match.group(5).strip()
        message = match.group(6).strip()
        full_message = f'{code}: {message}'
        key = (line_num, kind, full_message)
        if key in seen:
            continue
        seen.add(key)
        _add_diagnostic(errors, warnings, kind, line_num, full_message)

    return errors, warnings


def _parse_javascript_output(output):
    errors = []
    warnings = []
    seen = set()
    pending_line = 0

    for raw_line in output.splitlines():
        line = raw_line.strip()
        location_match = re.match(r'^(.*?):(\d+)(?::(\d+))?$', line)
        if location_match:
            pending_line = int(location_match.group(2))
            continue

        if any(token in line for token in ('SyntaxError:', 'ReferenceError:', 'TypeError:', 'RangeError:', 'EvalError:', 'URIError:')):
            message = line.split(':', 1)[1].strip() if ':' in line else line
            key = (pending_line, 'error', message)
            if key not in seen:
                seen.add(key)
                _add_diagnostic(errors, warnings, 'error', pending_line, message)
            pending_line = 0

    return errors, warnings


def _parse_python_output(output, source_path):
    errors = []
    warnings = []
    seen = set()
    pending_line = 0

    for raw_line in output.splitlines():
        line = raw_line.strip()

        file_match = re.match(r'^File "(.+)", line (\d+)(?:, in .+)?$', line)
        if file_match:
            pending_line = int(file_match.group(2))
            continue

        if any(token in line for token in ('SyntaxError:', 'IndentationError:', 'TabError:')):
            message = line.split(':', 1)[1].strip() if ':' in line else line
            key = (pending_line, 'error', message)
            if key not in seen:
                seen.add(key)
                _add_diagnostic(errors, warnings, 'error', pending_line, message)
            pending_line = 0

    return errors, warnings


def parse_diagnostics(language, output, file_path):
    if not output.strip():
        return [], []

    if language in {'c', 'cpp', 'java'}:
        return _parse_standard_output(output)
    if language == 'csharp':
        return _parse_csharp_output(output)
    if language == 'javascript':
        return _parse_javascript_output(output)
    if language == 'python':
        return _parse_python_output(output, file_path)
    return [], []


def _run_python_syntax_check(file_path):
    errors = []
    warnings = []

    try:
        with open(file_path, 'r', encoding='utf-8') as source_file:
            source = source_file.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as source_file:
            source = source_file.read()

    try:
        ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        errors.append({
            'line': exc.lineno or 0,
            'message': exc.msg or 'Syntax error',
            'type': 'error',
        })
    except Exception as exc:
        errors.append({
            'line': 0,
            'message': f'Analysis error: {exc}',
            'type': 'error',
        })

    return errors, warnings, ''


def analyze_syntax(file_path, language, repo_root=None, timeout=30):
    """Run a language-appropriate syntax check for a single file."""
    if language == 'python':
        return _run_python_syntax_check(file_path)

    errors = []
    warnings = []
    cleanup_paths = []
    compile_output = ''

    try:
        cmd, cleanup_paths = build_analysis_command(language, file_path, repo_root)
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        compile_output = '\n'.join(part for part in [process.stdout, process.stderr] if part)
        errors, warnings = parse_diagnostics(language, compile_output, file_path)

        if process.returncode != 0 and not errors:
            fallback_message = compile_output.strip() or f'{language} syntax check failed with exit code {process.returncode}'
            errors.append({
                'line': 0,
                'message': fallback_message,
                'type': 'error',
            })
    except subprocess.TimeoutExpired:
        errors.append({
            'line': 0,
            'message': f'Compilation timeout after {timeout} seconds',
            'type': 'error',
        })
    except FileNotFoundError:
        errors.append({
            'line': 0,
            'message': f'Compiler not found. Please install {get_missing_tool_hint(language)}.',
            'type': 'error',
        })
    except Exception as exc:
        errors.append({
            'line': 0,
            'message': f'Analysis error: {exc}',
            'type': 'error',
        })
    finally:
        for path in cleanup_paths:
            if os.path.isdir(path):
                try:
                    import shutil
                    shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    pass
            elif os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception:
                    pass

    return errors, warnings, compile_output


def compile_source(source_code, language=None, file_name=None, timeout=15):
    """Compile/check a source snippet without executing it.

    Language is inferred from ``file_name`` when possible.  Only a temporary
    source file is passed to the language compiler and it is always removed
    before returning.
    """
    if not isinstance(source_code, str):
        raise ValueError('Source code must be a string.')

    detected_language = detect_language(file_name) if file_name else None
    selected_language = normalize_language(language) or detected_language
    if not selected_language:
        raise ValueError(
            'Language is required when it cannot be detected from the filename. '
            'Supported languages: Java, Python, C++, and C.'
        )

    extension = os.path.splitext(file_name or '')[1].lower()
    if extension not in LANGUAGE_SPECS[selected_language]['extensions']:
        extension = LANGUAGE_SPECS[selected_language]['extensions'][0]

    safe_name = os.path.basename(file_name or f'Main{extension}')
    stem = os.path.splitext(safe_name)[0] or 'Main'

    # javac requires the source file to use the public type's name.
    if selected_language == 'java':
        public_type = re.search(
            r'\bpublic\s+(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)',
            source_code,
        )
        if public_type:
            stem = public_type.group(1)
        extension = '.java'

    temp_dir = tempfile.mkdtemp(prefix='code-compile-')
    source_path = os.path.join(temp_dir, f'{stem}{extension}')
    try:
        with open(source_path, 'w', encoding='utf-8', newline='') as source_file:
            source_file.write(source_code)

        errors, warnings, compile_output = analyze_syntax(
            source_path,
            selected_language,
            repo_root=temp_dir,
            timeout=timeout,
        )
        return {
            'language': selected_language,
            'language_label': language_label(selected_language),
            'file_name': os.path.basename(source_path),
            'success': not errors,
            'errors': errors,
            'warnings': warnings,
            'compile_output': compile_output,
        }
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
