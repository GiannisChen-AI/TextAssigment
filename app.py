from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
import os
import re
import string
from collections import defaultdict, Counter
import sqlite3
from werkzeug.utils import secure_filename
import requests
from bs4 import BeautifulSoup
import zipfile
import io

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# 配置文件上传
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# 创建必要的目录
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('data', exist_ok=True)


class DocumentSearchEngine:
    def __init__(self):
        self.stop_words = self.load_stop_words()
        self.inverted_index = defaultdict(list)
        self.documents = {}
        self.init_database()

    def load_stop_words(self):
        """加载停用词"""
        stop_words = set()
        try:
            # 从提供的停用词文件加载
            with open('StopWords.txt', 'r', encoding='utf-8') as f:
                content = f.read()
                # 解析停用词，去除引号和空格
                words = re.findall(r'"([^"]*)"', content)
                stop_words.update(word.lower().strip() for word in words if word.strip())
        except FileNotFoundError:
            # 如果文件不存在，使用默认停用词
            stop_words = {
                'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'have',
                'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should',
                'i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him', 'her', 'us', 'them'
            }
        return stop_words

    def init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect('data/documents.db')
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                word_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS word_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                document_id INTEGER,
                position INTEGER,
                line_number INTEGER,
                FOREIGN KEY (document_id) REFERENCES documents (id)
            )
        ''')

        conn.commit()
        conn.close()

    def preprocess_text(self, text):
        """文本预处理"""
        # 转换为小写
        text = text.lower()

        # 移除标点符号
        text = text.translate(str.maketrans('', '', string.punctuation))

        # 移除非ASCII字符
        text = ''.join(char for char in text if ord(char) < 128)

        # 分词
        words = text.split()

        # 移除停用词
        words = [word for word in words if word not in self.stop_words and len(word) > 1]

        return words

    def simple_stemming(self, word):
        """简单的词干提取"""
        # 基本的英语词干提取规则
        if word.endswith('ing'):
            return word[:-3]
        elif word.endswith('ed'):
            return word[:-2]
        elif word.endswith('s') and len(word) > 3:
            return word[:-1]
        return word

    def add_document(self, filename, content):
        """添加文档到索引"""
        conn = sqlite3.connect('data/documents.db')
        cursor = conn.cursor()

        # 保存文档
        cursor.execute(
            'INSERT INTO documents (filename, content, word_count) VALUES (?, ?, ?)',
            (filename, content, len(content.split()))
        )
        doc_id = cursor.lastrowid

        # 处理文档内容
        lines = content.split('\n')
        position = 0

        for line_num, line in enumerate(lines, 1):
            words = self.preprocess_text(line)

            for word in words:
                stemmed_word = self.simple_stemming(word)

                # 添加到索引
                cursor.execute(
                    'INSERT INTO word_index (word, document_id, position, line_number) VALUES (?, ?, ?, ?)',
                    (stemmed_word, doc_id, position, line_num)
                )
                position += 1

        conn.commit()
        conn.close()

        return doc_id

    def search(self, query, max_results=10):
        """搜索文档"""
        query_words = self.preprocess_text(query)
        if not query_words:
            return []

        # 对查询词进行词干提取
        query_words = [self.simple_stemming(word) for word in query_words]

        conn = sqlite3.connect('data/documents.db')
        cursor = conn.cursor()

        # 搜索包含查询词的文档
        placeholders = ', '.join(['?' for _ in query_words])
        cursor.execute(f'''
            SELECT d.id, d.filename, d.content, 
                   COUNT(wi.word) as relevance_score,
                   GROUP_CONCAT(wi.line_number) as line_numbers
            FROM documents d
            JOIN word_index wi ON d.id = wi.document_id
            WHERE wi.word IN ({placeholders})
            GROUP BY d.id
            ORDER BY relevance_score DESC
            LIMIT ?
        ''', query_words + [max_results])

        results = []
        for row in cursor.fetchall():
            doc_id, filename, content, score, line_numbers = row

            # 获取匹配的行
            lines = content.split('\n')
            line_nums = list(map(int, line_numbers.split(',')))
            matching_lines = []

            for line_num in set(line_nums):
                if line_num <= len(lines):
                    line_content = lines[line_num - 1]
                    # 高亮查询词
                    for word in query_words:
                        pattern = re.compile(re.escape(word), re.IGNORECASE)
                        line_content = pattern.sub(f'<mark>{word}</mark>', line_content)

                    matching_lines.append({
                        'line_number': line_num,
                        'content': line_content
                    })

            results.append({
                'document_id': doc_id,
                'filename': filename,
                'relevance_score': score,
                'matching_lines': matching_lines[:5]  # 只显示前5个匹配行
            })

        conn.close()
        return results


# 初始化搜索引擎
search_engine = DocumentSearchEngine()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'files' not in request.files:
            flash('没有选择文件')
            return redirect(request.url)

        files = request.files.getlist('files')
        uploaded_count = 0

        for file in files:
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                # 读取文件内容
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()

                    # 添加到搜索引擎
                    search_engine.add_document(filename, content)
                    uploaded_count += 1

                except Exception as e:
                    flash(f'处理文件 {filename} 时出错: {str(e)}')

        flash(f'成功上传并处理了 {uploaded_count} 个文件')
        return redirect(url_for('index'))

    return render_template('upload.html')


@app.route('/search', methods=['POST'])
def search():
    query = request.form.get('query', '').strip()
    if not query:
        return jsonify({'error': '请输入搜索词'})

    try:
        results = search_engine.search(query)
        return jsonify({
            'query': query,
            'results': results,
            'total': len(results)
        })
    except Exception as e:
        return jsonify({'error': f'搜索时出错: {str(e)}'})


@app.route('/download_gutenberg')
def download_gutenberg():
    """从古腾堡项目下载示例文档"""
    try:
        # 一些经典文本的URL
        gutenberg_texts = [
            ('https://www.gutenberg.org/files/74/74-0.txt', 'tom_sawyer.txt'),
            ('https://www.gutenberg.org/files/1342/1342-0.txt', 'pride_and_prejudice.txt'),
            ('https://www.gutenberg.org/files/11/11-0.txt', 'alice_wonderland.txt')
        ]

        downloaded_count = 0
        for url, filename in gutenberg_texts:
            try:
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    content = response.text
                    # 移除古腾堡项目的头部和尾部信息
                    content = re.sub(r'\*\*\*.*?\*\*\*', '', content, flags=re.DOTALL)

                    # 添加到搜索引擎
                    search_engine.add_document(filename, content)
                    downloaded_count += 1
            except:
                continue

        flash(f'成功下载并处理了 {downloaded_count} 个古腾堡项目文档')
    except Exception as e:
        flash(f'下载文档时出错: {str(e)}')

    return redirect(url_for('index'))


@app.route('/stats')
def stats():
    """显示系统统计信息"""
    conn = sqlite3.connect('data/documents.db')
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM documents')
    doc_count = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(DISTINCT word) FROM word_index')
    unique_words = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM word_index')
    total_words = cursor.fetchone()[0]

    cursor.execute('''
        SELECT word, COUNT(*) as frequency 
        FROM word_index 
        GROUP BY word 
        ORDER BY frequency DESC 
        LIMIT 10
    ''')
    top_words = cursor.fetchall()

    conn.close()

    return jsonify({
        'document_count': doc_count,
        'unique_words': unique_words,
        'total_words': total_words,
        'top_words': top_words
    })


if __name__ == '__main__':
    app.run(debug=True)