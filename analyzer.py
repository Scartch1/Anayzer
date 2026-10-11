import os
from config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from language_support import analyze_syntax, detect_language

class CodeAnalyzer:
    def __init__(self):
        self.compile_timeout = Config.COMPILE_TIMEOUT
        
    def find_source_files(self, repo_path):
        """Find all supported source files in repository"""
        source_files = []
        
        for root, dirs, files in os.walk(repo_path):
            # Skip hidden directories and common build directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['build', 'dist', 'node_modules']]
            
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, repo_path)

                language = detect_language(file)
                if language:
                    source_files.append((file_path, rel_path, language))
        
        return source_files

    def analyze_file(self, file_path, language, repo_root=None):
        """Analyze a single source file."""
        errors, warnings, compile_output = analyze_syntax(
            file_path,
            language,
            repo_root=repo_root,
            timeout=self.compile_timeout
        )
        # A missing compiler means this file was not checked; it is not a code
        # error and should not fail the repository analysis result.
        unavailable = [item for item in errors if item.get('type') == 'unavailable']
        if unavailable:
            errors = [item for item in errors if item.get('type') != 'unavailable']
            warnings.extend(unavailable)
        return {
            'errors': errors,
            'warnings': warnings,
            'compile_output': compile_output,
            'passed': len(errors) == 0
        }

    def analyze_repository(self, repo_path, submission_id, db, max_workers=4):
        """Analyze all supported source files in repository"""
        files = self.find_source_files(repo_path)
        
        if not files:
            return {'total_files': 0, 'analyzed_files': 0}
        
        # Update submission with total files
        db.submissions.update_one(
            {'_id': submission_id},
            {'$set': {'total_files': len(files), 'status': 'analyzing'}}
        )
        
        results = []
        total_errors = 0
        total_warnings = 0
        
        # Analyze files in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {
                executor.submit(self.analyze_file, file_path, language, repo_path): (file_path, rel_path, language)
                for file_path, rel_path, language in files
            }
            
            analyzed_count = 0
            for future in as_completed(future_to_file):
                file_path, rel_path, language = future_to_file[future]
                
                try:
                    analysis_result = future.result()
                    
                    # Store in database
                    result_doc = {
                        'submission_id': submission_id,
                        'file_path': rel_path,
                        'file_name': os.path.basename(file_path),
                        'language': language,
                        'status': 'analyzed',
                        'errors': analysis_result['errors'],
                        'warnings': analysis_result['warnings'],
                        'compile_output': analysis_result['compile_output'],
                        'analyzed_at': datetime.utcnow(),
                        'passed': analysis_result['passed']
                    }
                    
                    db.analysis_results.insert_one(result_doc)
                    
                    total_errors += len(analysis_result['errors'])
                    total_warnings += len(analysis_result['warnings'])
                    
                    analyzed_count += 1
                    
                    # Update submission progress
                    db.submissions.update_one(
                        {'_id': submission_id},
                        {
                            '$set': {
                                'analyzed_files': analyzed_count,
                                'errors_count': total_errors,
                                'warnings_count': total_warnings
                            }
                        }
                    )
                    
                except Exception as e:
                    print(f"Error analyzing {rel_path}: {e}")
        
        # Mark submission as completed
        db.submissions.update_one(
            {'_id': submission_id},
            {
                '$set': {
                    'status': 'completed',
                    'completed_at': datetime.utcnow()
                }
            }
        )
        
        return {
            'total_files': len(files),
            'analyzed_files': analyzed_count,
            'total_errors': total_errors,
            'total_warnings': total_warnings
        }
