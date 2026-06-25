"""
RepoSummarizer - Autonomous Codebase Intelligence Agent

A specialized CLI tool designed to clone a GitHub repository and perform structural
analysis to generate a natural language summary of the codebase. It utilizes Python's
standard library (AST, Regex, Collections) for static analysis and symbol extraction,
functioning as a "local AI" that interprets code structure without heavy external ML
dependencies. It gracefully integrates with LLMs if an API key is present.

Usage:
    python repo_summarizer.py https://github.com/psf/requests
    python repo_summarizer.py https://github.com/torvalds/linux --depth 1

Attributes:
    AUTHOR: Compounding Asset Specialist
    VERSION: 1.0.0
    DEPENDENCIES: Python 3.8+, requests (stdlib + requests)
"""

import argparse
import ast
import csv
import io
import logging
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# -----------------------------------------------------------------------------
# Configuration and Constants
# -----------------------------------------------------------------------------

# Standard English stopwords + common programming stopwords for NLP filtering
STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "arent", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "cant", "cannot", "could", "couldnt",
    "did", "didnt", "do", "does", "doesnt", "doing", "dont", "down", "during",
    "each", "few", "for", "from", "further", "had", "hadnt", "has", "hasnt", "have",
    "havent", "having", "he", "hed", "hell", "hes", "her", "here", "heres", "hers",
    "herself", "him", "himself", "his", "how", "hows", "i", "id", "ill", "im",
    "ive", "if", "in", "into", "is", "isnt", "it", "its", "its", "itself", "lets",
    "me", "more", "most", "mustnt", "my", "myself", "no", "nor", "not", "of", "off",
    "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves", "out",
    "over", "own", "same", "shant", "she", "shed", "shell", "shes", "should",
    "shouldnt", "so", "some", "such", "than", "that", "thats", "the", "their", "theirs",
    "them", "themselves", "then", "there", "theres", "these", "they", "theyd", "theyll",
    "theyre", "theyve", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "wasnt", "we", "wed", "well", "were", "weve", "were", "werent",
    "what", "whats", "when", "whens", "where", "wheres", "which", "while", "who",
    "whos", "whom", "why", "whys", "with", "wont", "would", "wouldnt", "you", "youd",
    "youll", "youre", "youve", "your", "yours", "yourself", "yourselves", "self", 
    "args", "kwargs", "return", "true", "false", "print", "def", "class", "if", 
    "else", "elif", "import", "from", "none"
}

SUPPORTED_EXTENSIONS = {".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".go", ".rs"}
MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1MB limit per file to prevent overload

# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

class DependencyType(Enum):
    REQUIREMENTS = "requirements.txt"
    SETUP_PY = "setup.py"
    PACKAGE_JSON = "package.json"
    GO_MOD = "go.mod"
    CARGO_TOML = "Cargo.toml"
    UNKNOWN = "unknown"

@dataclass
class CodeStructure:
    """Represents the structural analysis of a specific file."""
    file_path: str
    language: str
    classes: List[Dict[str, Any]] = field(default_factory=list)
    functions: List[Dict[str, Any]] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    line_count: int = 0

@dataclass
class RepoAnalysis:
    """Aggregates analysis data for the entire repository."""
    repo_name: str
    url: str
    total_files: int = 0
    structures: List[CodeStructure] = field(default_factory=list)
    dependencies: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    top_keywords: List[Tuple[str, int]] = field(default_factory=list)

# -----------------------------------------------------------------------------
# Core Logic: Git and File Handling
# -----------------------------------------------------------------------------

class GitHubHandler:
    """Handles GitHub repository retrieval using standard libraries + requests."""

    def __init__(self, url: str):
        self.url = url
        self.repo_name = self._parse_repo_name(url)
        self.temp_dir = tempfile.mkdtemp(prefix=f"summarizer_{self.repo_name}_")

    def _parse_repo_name(self, url: str) -> str:
        """Extracts repository name from URL."""
        match = re.search(r"github\.com/([^/]+)/([^/]+?)(\.git)?$", url)
        if not match:
            raise ValueError("Invalid GitHub URL format.")
        return match.group(2)

    def clone(self) -> Path:
        """
        Clones the repository by downloading the Zipball.
        Simulates a clone by fetching an archive to avoid requiring `git` binary.
        """
        logging.info(f"Acquiring asset: {self.repo_name} from {self.url}")
        
        # Construct the API URL for zipball
        api_url = f"https://api.github.com/repos/{re.search(r'github\.com/([^/]+)/([^/]+)', self.url).group(1)}/{self.repo_name}/zipball"
        
        try:
            response = requests.get(api_url, stream=True, timeout=30)
            response.raise_for_status()
            
            # Save zip to memory first to handle cleanup easily
            zip_data = io.BytesIO(response.content)
            
            with zipfile.ZipFile(zip_data) as zip_ref:
                # The repo name in the zipball usually has a hash prefix, e.g. {user}-{repo}-{hash}
                # We extract all contents to the root of our temp dir
                zip_ref.extractall(self.temp_dir)
                
            # Handle the nested folder structure created by GitHub zipballs
            extracted_root = Path(self.temp_dir)
            inner_folders = [f for f in extracted_root.iterdir() if f.is_dir()]
            if inner_folders:
                return inner_folders[0]
            return extracted_root

        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Failed to acquire repository: {e}") from e

    def cleanup(self):
        """Removes temporary files."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            logging.info(f"Cleaned up temporary assets: {self.temp_dir}")

# -----------------------------------------------------------------------------
# Core Logic: Static Analysis (The "AI")
# -----------------------------------------------------------------------------

class ASTAnalyzer:
    """Performs Abstract Syntax Tree analysis on Python files."""

    @staticmethod
    def analyze_python(file_path: Path) -> CodeStructure:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
                
            tree = ast.parse(source)
            structure = CodeStructure(
                file_path=str(file_path.relative_to(file_path.parents[1])), # Assumes root context
                language="Python",
                docstring=ast.get_docstring(tree),
                line_count=len(source.splitlines())
            )

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    methods = []
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            methods.append(item.name)
                    
                    structure.classes.append({
                        "name": node.name,
                        "methods": methods,
                        "docstring": ast.get_docstring(node)
                    })
                
                elif isinstance(node, ast.FunctionDef):
                    # Check if nested in a class to avoid double counting (simple check)
                    is_method = any(isinstance(parent, ast.ClassDef) for parent in ast.walk(tree) if hasattr(parent, 'body') and node in parent.body)
                    if not is_method:
                        structure.functions.append({
                            "name": node.name,
                            "args": [a.arg for a in node.args.args],
                            "docstring": ast.get_docstring(node)
                        })
                
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        structure.imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        structure.imports.append(f"{node.module}.{alias.name}" if node.module else alias.name)
            
            return structure

        except SyntaxError:
            logging.debug(f"Skipping non-parseable Python file: {file_path}")
            return CodeStructure(file_path=str(file_path), language="Python (Invalid)")

class HeuristicAnalyzer:
    """
    Performs regex-based analysis for non-Python languages.
    This is 'symbolic AI' - pattern matching to infer structure.
    """

    @staticmethod
    def analyze_generic(file_path: Path) -> CodeStructure:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            ext = file_path.suffix.lower()
            lang_map = {".js": "JavaScript", ".ts": "TypeScript", ".java": "Java", ".c": "C", ".cpp": "C++", ".go": "Go"}
            lang = lang_map.get(ext, "Unknown")
            
            structure = CodeStructure(
                file_path=str(file_path.relative_to(file_path.parents[1])),
                language=lang,
                line_count=len(content.splitlines())
            )

            # Regex for Functions
            # Matches: 'function name', 'def name', 'public type name', 'func name'
            func_pattern = re.compile(
                r'\b(function|def|func)\s+([a-zA-Z0-9_]+)|'  # func, def, function name
                r'\b(public|private|protected)?\s*(static)?\s*\w+\s+([a-zA-Z0-9_]+)\s*\(', # Java/C++ style
                re.MULTILINE
            )
            
            for match in func_pattern.finditer(content):
                name = match.group(2) or match.group(4)
                if name and not name.startswith("_"): # Simple heuristic for internal
                    structure.functions.append({"name": name, "args": []})

            # Regex for Classes
            class_pattern = re.compile(r'\bclass\s+([a-zA-Z0-9_]+)')
            for match in class_pattern.finditer(content):
                structure.classes.append({"name": match.group(1), "methods": []})

            return structure

        except Exception as e:
            logging.debug(f"Error analyzing generic file {file_path}: {e}")
            return CodeStructure(file_path=str(file_path), language="Error")

class DependencyScanner:
    """Scans for dependency files."""

    @staticmethod
    def scan(root_path: Path) -> Dict[str, List[str]]:
        deps = defaultdict(list)
        
        # Check for requirements.txt
        req_file = root_path / "requirements.txt"
        if req_file.exists():
            with open(req_file, "r", encoding="utf-8", errors="ignore") as f:
                deps[DependencyType.REQUIREMENTS.value] = [
                    line.split("==")[0].strip().lower() 
                    for line in f 
                    if line.strip() and not line.startswith("#")
                ]

        # Check for setup.py (Basic extraction of install_requires)
        setup_file = root_path / "setup.py"
        if setup_file.exists():
            with open(setup_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                # Very naive regex extraction of install_requires list items
                install_items = re.findall(r'install_requires\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if install_items:
                    items = re.findall(r'["\']([^"\']+)["\']', install_items[0])
                    deps[DependencyType.SETUP_PY.value] = [i.split(">")[0].split("<")[0].split("=")[0].strip() for i in items]

        # Check for package.json
        pkg_file = root_path / "package.json"
        if pkg_file.exists():
            try:
                import json
                with open(pkg_file, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
                    deps[DependencyType.PACKAGE_JSON.value] = list(data.get("dependencies", {}).keys())
            except json.JSONDecodeError:
                pass

        return deps

# -----------------------------------------------------------------------------
# Core Logic: NLP and Summarization
# -----------------------------------------------------------------------------

class SummarizationEngine:
    """Generates natural language summaries using standard NLP techniques."""

    def __init__(self, analysis: RepoAnalysis):
        self.analysis = analysis

    def extract_keywords(self) -> List[Tuple[str, int]]:
        """
        Extracts significant keywords from identifiers and docstrings.
        Uses Term Frequency filtering against STOP_WORDS.
        """
        words = []
        
        for struct in self.analysis.structures:
            # From Class names (split camel case)
            for cls in struct.classes:
                words.extend(re.findall(r'[A-Z]?[a-z]+', cls['name']))
            
            # From Function names
            for func in struct.functions:
                words.extend(re.findall(r'[A-Z]?[a-z]+', func['name']))
            
            # From Docstrings
            if struct.docstring:
                words.extend(re.findall(r'\b[a-zA-Z]{3,}\b', struct.docstring.lower()))

        # Filter and count
        filtered = [w.lower() for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
        return Counter(filtered).most_common(10)

    def generate_symbolic_summary(self) -> str:
        """
        Generates a summary using hardcoded rules and templates based on AST data.
        This is the 'graceful degradation' path if no LLM API key is available.
        """
        total_classes = sum(len(s.classes) for s in self.analysis.structures)
        total_funcs = sum(len(s.functions) for s in self.analysis.structures)
        langs = set(s.language for s in self.analysis.structures)
        
        key_deps = []
        for dtype, items in self.analysis.dependencies.items():
            key_deps.extend(items[:3]) # Take top 3 from each
        
        report_lines = [
            "# Codebase Summary Report\n",
            f"**Repository:** {self.analysis.repo_name}",
            f"**Primary Languages:** {', '.join(langs)}",
            f"**Composition:** {total_classes} classes, {total_funcs} functions identified.",
            ""
        ]

        if self.analysis.top_keywords:
            keywords_str = ", ".join([k for k, _ in self.analysis.top_keywords])
            report_lines.append(f"**Key Concepts:** {keywords_str}\n")

        if key_deps:
            report_lines.append("**Core Dependencies:**")
            for d in list(set(key_deps)):
                report_lines.append(f"- {d}")
            report_lines.append("")

        # Heuristic: Identify Main entry points (files with no prefix or named main/app)
        main_files = [s for s in self.analysis.structures if 'main' in s.file_path.lower() or 'app' in s.file_path.lower()]
        
        report_lines.append("## Architecture Highlights")
        if main_files:
            report_lines.append(f"Identified potential entry points:")
            for mf in main_files[:3]:
                report_lines.append(f"- `{mf.file_path}`")
        
        # List most complex files
        sorted_structs = sorted(self.analysis.structures, key=lambda x: x.line_count, reverse=True)
        report_lines.append("\n## Largest Components")
        for s in sorted_structs[:3]:
            report_lines.append(f"- **{s.file_path}**: {s.line_count} lines, {len(s.classes)} classes.")

        return "\n".join(report_lines)

    def generate_llm_summary(self) -> str:
        """
        Calls OpenAI API to synthesize the AST data into a coherent summary.
        """
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return "No OPENAI_API_KEY found. Falling back to symbolic summary."

        # Prepare a compressed representation of the repo for the prompt
        context = []
        for s in self.analysis.structures[:10]: # Limit context to prevent overflow
            context.append(f"File: {s.file_path} ({s.language})")
            for c in s.classes[:2]:
                context.append(f"  Class: {c['name']}")
            for f in s.functions[:2]:
                context.append(f"  Func: {f['name']}")
        
        prompt_text = f"""
        Analyze the following code structure data from GitHub repo '{self.analysis.repo_name}'.
        Provide a 3-sentence summary explaining:
        1. What the project likely does based on class/function names.
        2. The main technology stack.
        3. Key functionality.
        
        Data:
        {chr(10).join(context)}
        
        Keywords: {', '.join([k for k,_ in self.analysis.top_keywords])}
        Dependencies: {list(self.analysis.dependencies.keys())[:5]}
        """

        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "system", "content": "You are an expert code analyst. Be concise."}, 
                             {"role": "user", "content": prompt_text}],
                "temperature": 0.3
            }
            
            response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            result = response.json()
            
            llm_content = result['choices'][0]['message']['content']
            return f"## AI-Generated Synopsis\n{llm_content}\n\n{self.generate_symbolic_summary()}"

        except Exception as e:
            logging.warning(f"LLM Generation failed: {e}. Degrading gracefully.")
            return self.generate_symbolic_summary()

# -----------------------------------------------------------------------------
# Orchestration and CLI
# -----------------------------------------------------------------------------

def run_analysis(url: str, use_llm: bool = False) -> str:
    git_handler = GitHubHandler(url)
    repo_root = None
    
    try:
        # 1. Acquire Asset
        repo_root = git_handler.clone()
        logging.info(f"Asset acquired at: {repo_root}")

        # 2. Scan and Parse
        all_structures = []
        for file_path in repo_root.rglob("*"):
            if file_path.is_file() and file_path.suffix in SUPPORTED_EXTENSIONS:
                if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    logging.debug(f"Skipping large file: {file_path}")
                    continue
                
                logging.debug(f"Analyzing: {file_path}")
                
                if file_path.suffix == ".py":
                    struct = ASTAnalyzer.analyze_python(file_path)
                else:
                    struct = HeuristicAnalyzer.analyze_generic(file_path)
                
                all_structures.append(struct)

        # 3. Identify Dependencies
        dependencies = DependencyScanner.scan(repo_root)

        # 4. Build Analysis Object
        repo_analysis = RepoAnalysis(
            repo_name=git_handler.repo_name,
            url=url,
            total_files=len(all_structures),
            structures=all_structures,
            dependencies=dependencies
        )

        # 5. NLP Processing
        engine = SummarizationEngine(repo_analysis)
        repo_analysis.top_keywords = engine.extract_keywords()

        # 6. Generate Final Report
        if use_llm:
            return engine.generate_llm_summary()
        else:
            return engine.generate_symbolic_summary()

    finally:
        git_handler.cleanup()

def main():
    parser = argparse.ArgumentParser(
        description="RepoSummarizer: CLI tool for AI-powered code summarization using stdlib.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("repo_url", help="GitHub repository URL to analyze")
    parser.add_argument("--llm", action="store_true", help="Enable OpenAI API for higher-level summarization (requires OPENAI_API_KEY env var)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    try:
        print("Initializing RepoSummarizer Agent...")
        summary = run_analysis(args.repo_url, use_llm=args.llm)
        print("\n" + "="*60)
        print(summary)
        print("="*60)
        
    except ValueError as ve:
        print(f"Input Error: {ve}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as ce:
        print(f"Network Error: {ce}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected System Failure: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    import sys
    main()