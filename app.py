import os
import re
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename
import pdfplumber
import docx

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}

STOPWORDS = set([
    'the','a','an','and','or','but','in','on','at','to','for','of','with',
    'is','are','was','were','be','been','being','have','has','had','do','does',
    'did','will','would','could','should','may','might','shall','can','this',
    'that','these','those','it','its','by','from','as','into','through','during',
    'before','after','above','below','between','each','so','such','than','too',
    'very','just','also','about','up','out','if','then','there','when','where',
    'which','who','whom','how','all','both','few','more','most','other','some',
    'any','only','same','own','not','no','nor','he','she','they','we','you','i',
    'our','their','your','his','her','my','am','us','me','him',
    'what','page','figure','table','section','chapter','note','example','see',
])

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(filepath, ext):
    text = ""
    if ext == 'txt':
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    elif ext == 'pdf':
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                pt = page.extract_text()
                if pt:
                    text += pt + "\n"
    elif ext == 'docx':
        doc = docx.Document(filepath)
        for para in doc.paragraphs:
            text += para.text + "\n"
    return text

def split_into_sentences(text):
    sentences = re.split(r'(?<=[.!?])\s+', text)
    result = []
    for s in sentences:
        parts = [p.strip() for p in s.split('\n') if p.strip()]
        result.extend(parts)
    return [s for s in result if len(s) > 5]

# ─────────────────────────────────────────────
#  HEADING DETECTION
# ─────────────────────────────────────────────

def is_heading(line):
    """Strict heading detection to get correct topic headings."""
    line = line.strip()
    if not line or len(line) > 100:
        return False
    # Skip lines that are clearly sentences (contain verb patterns)
    if re.search(r'\b(is|are|was|were|has|have|had|will|would|can|could|should)\b.{10,}', line):
        return False
    # Numbered heading: 1. Title or 1.1 Title
    if re.match(r'^\d+[\.\d]*\s+[A-Za-z]', line):
        return True
    # ALL CAPS (min 4 chars, max 8 words)
    if line.isupper() and 3 < len(line) <= 80 and len(line.split()) <= 8:
        return True
    # Ends with colon, short
    if line.endswith(':') and len(line.split()) <= 7 and len(line) > 4:
        return True
    # Title Case: most words capitalized, no ending period, short
    words = line.split()
    cap_count = sum(1 for w in words if w and w[0].isupper())
    if (3 <= len(words) <= 8 and
            not line.endswith('.') and
            cap_count >= len(words) * 0.7):
        return True
    return False

# ─────────────────────────────────────────────
#  CONCISE POINT EXTRACTION
# ─────────────────────────────────────────────

def condense_sentence(sentence):
    """
    Shorten a sentence into a concise study point.
    - Remove filler phrases
    - Keep core meaning
    - Max ~20 words
    """
    s = sentence.strip()

    # Remove common filler starts
    fillers = [
        r'^(it is|it was|there is|there are|this is|this was|we can say that|note that|it should be noted that|it must be noted that)\s+',
        r'^(in other words|that is to say|as mentioned above|as stated above)\s*,?\s*',
        r'^(therefore|thus|hence|so|consequently|as a result),?\s+',
    ]
    for f in fillers:
        s = re.sub(f, '', s, flags=re.IGNORECASE).strip()

    # Capitalize first letter
    if s:
        s = s[0].upper() + s[1:]

    # If still too long (>22 words), trim at a natural break
    words = s.split()
    if len(words) > 22:
        # Try to cut at a comma or semicolon within first 22 words
        short = ' '.join(words[:22])
        cut = max(short.rfind(','), short.rfind(';'))
        if cut > 20:
            s = short[:cut].strip()
        else:
            s = short.strip()
        if not s.endswith('.'):
            s += '.'

    return s

def is_definition(s):
    return bool(re.search(
        r'\b(is defined as|is called|refers to|stands for|denotes|known as|abbreviated as|is an?\s|are the\s|is the\s)\b',
        s, re.IGNORECASE))

def is_key_fact(s):
    return bool(re.search(
        r'\b(used for|used to|enables|allows|provides|performs|generates|supports|'
        r'advantage|disadvantage|feature|property|function|purpose|'
        r'important|key|must|always|never|critical|essential|\d+[\-\s]bit|'
        r'step \d|phase \d|type of|kind of|consist|contain|include)\b',
        s, re.IGNORECASE))

def build_word_freq(text):
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    freq = {}
    for w in words:
        if w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    max_f = max(freq.values()) if freq else 1
    return {w: v / max_f for w, v in freq.items()}

def score_sentence(s, word_freq):
    words = re.findall(r'\b[a-zA-Z]{3,}\b', s.lower())
    score = sum(word_freq.get(w, 0) for w in words if w not in STOPWORDS)
    length = len(words)
    if length < 4:    score *= 0.2
    elif length > 40: score *= 0.5
    if is_definition(s):  score *= 2.0
    if is_key_fact(s):    score *= 1.6
    return score

def extract_concise_points(sentences, word_freq, max_defs=8, max_kp=12):
    """Extract and condense definitions + key points separately."""
    definitions = []
    key_points  = []
    seen = set()

    # Score all sentences
    scored = sorted(sentences, key=lambda s: score_sentence(s, word_freq), reverse=True)

    for s in scored:
        if len(s.split()) < 4:
            continue
        norm = s.lower()[:60]
        if norm in seen:
            continue
        seen.add(norm)

        point = condense_sentence(s)
        if not point:
            continue

        if is_definition(s) and len(definitions) < max_defs:
            definitions.append(point)
        elif is_key_fact(s) and len(key_points) < max_kp:
            key_points.append(point)
        elif len(key_points) < max_kp and score_sentence(s, word_freq) > 0.3:
            key_points.append(point)

        if len(definitions) >= max_defs and len(key_points) >= max_kp:
            break

    return definitions, key_points

# ─────────────────────────────────────────────
#  SECTION PARSER
# ─────────────────────────────────────────────

def parse_sections(raw_text):
    lines = [l.strip() for l in raw_text.split('\n')]
    sections = []
    current_title = None
    current_lines = []

    for line in lines:
        if not line:
            continue
        if is_heading(line):
            if current_lines and current_title:
                sections.append({'title': current_title, 'lines': current_lines})
            current_title = line.rstrip(':').strip()
            current_lines = []
        else:
            if current_title is None:
                current_title = "Overview"
            current_lines.append(line)

    if current_lines and current_title:
        sections.append({'title': current_title, 'lines': current_lines})

    # Merge tiny sections into previous
    merged = []
    for sec in sections:
        if merged and len(sec['lines']) < 3:
            merged[-1]['lines'].extend(sec['lines'])
        else:
            merged.append(sec)

    return merged if merged else [{'title': 'Document Notes', 'lines': lines}]

# ─────────────────────────────────────────────
#  MAIN NOTE GENERATOR
# ─────────────────────────────────────────────

def generate_structured_notes(text):
    word_freq = build_word_freq(text)

    # Key topics
    sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
    key_topics = [w for w, _ in sorted_words if len(w) > 3][:15]

    sections = parse_sections(text)
    note_sections = []

    for sec in sections:
        raw = ' '.join(sec['lines'])
        sentences = split_into_sentences(raw)
        # Also pick up bullet-style short lines
        bullets = [l for l in sec['lines']
                   if re.match(r'^\s*[\-\•\*]\s*.+', l) and len(l.split()) >= 3]
        all_candidates = sentences + [b for b in bullets if b not in sentences]

        defs, kps = extract_concise_points(all_candidates, word_freq)

        if defs or kps:
            note_sections.append({
                'title': sec['title'],
                'definitions': defs,
                'key_points': kps,
                'sentence_count': len(sentences),
            })

    # Fallback: no sections detected
    if not note_sections:
        all_sentences = split_into_sentences(text)
        defs, kps = extract_concise_points(all_sentences, word_freq)
        note_sections = [{
            'title': 'Study Notes',
            'definitions': defs,
            'key_points': kps,
            'sentence_count': len(all_sentences),
        }]

    total_points = sum(len(s['definitions']) + len(s['key_points']) for s in note_sections)

    return {
        'sections': note_sections,
        'key_topics': key_topics,
        'word_count': len(text.split()),
        'total_sentences': len(split_into_sentences(text)),
        'total_points': total_points,
        'section_count': len(note_sections),
    }

# ─────────────────────────────────────────────
#  WORD SEARCH
# ─────────────────────────────────────────────

def find_word_context(text, word):
    word_lower = word.lower().strip()
    sentences = split_into_sentences(text)
    pattern = re.compile(r'\b' + re.escape(word_lower) + r'\b', re.IGNORECASE)
    matched_sentences = [s for s in sentences if pattern.search(s)]
    frequency = len(pattern.findall(text))
    if frequency == 0:
        return None, 0
    def_pat = re.compile(
        r'\b(is|are|means|refers to|defined as|describes|denotes|stands for|represents|is called|known as)\b',
        re.IGNORECASE)
    def_sents = [s for s in matched_sentences if def_pat.search(s)]
    primary = def_sents[0] if def_sents else matched_sentences[0]
    supporting = [s for s in matched_sentences if s != primary][:6]
    return {
        "word": word, "frequency": frequency,
        "primary_context": primary, "supporting_contexts": supporting,
        "total_sentences_found": len(matched_sentences),
    }, frequency

# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Unsupported file type. Use PDF, DOCX, or TXT.'}), 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    ext = filename.rsplit('.', 1)[1].lower()
    try:
        text = extract_text_from_file(filepath, ext)
    except Exception as e:
        return jsonify({'error': f'Failed to parse file: {str(e)}'}), 500
    if not text.strip():
        return jsonify({'error': 'File appears to be empty or unreadable.'}), 400
    text_file = filepath + '.extracted.txt'
    with open(text_file, 'w', encoding='utf-8') as f:
        f.write(text)
    return jsonify({
        'success': True, 'filename': filename,
        'word_count': len(text.split()),
        'sentence_count': len(split_into_sentences(text)),
        'text_key': filename,
    })

@app.route('/search', methods=['POST'])
def search_word():
    data = request.get_json()
    word = data.get('word', '').strip()
    filename = data.get('filename', '').strip()
    if not word:
        return jsonify({'error': 'Please enter a word to search.'}), 400
    if not filename:
        return jsonify({'error': 'No document loaded.'}), 400
    safe_filename = secure_filename(filename)
    text_file = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename + '.extracted.txt')
    if not os.path.exists(text_file):
        return jsonify({'error': 'Document session expired. Please re-upload.'}), 404
    with open(text_file, 'r', encoding='utf-8') as f:
        text = f.read()
    result, frequency = find_word_context(text, word)
    if frequency == 0:
        return jsonify({'found': False, 'word': word,
                        'message': f'The word "{word}" was not found in the document.'})
    return jsonify({'found': True, **result})

@app.route('/summarize', methods=['POST'])
def summarize_document():
    data = request.get_json()
    filename = data.get('filename', '').strip()
    if not filename:
        return jsonify({'error': 'No document loaded.'}), 400
    safe_filename = secure_filename(filename)
    text_file = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename + '.extracted.txt')
    if not os.path.exists(text_file):
        return jsonify({'error': 'Document session expired. Please re-upload.'}), 404
    with open(text_file, 'r', encoding='utf-8') as f:
        text = f.read()
    try:
        notes = generate_structured_notes(text)
        return jsonify({'success': True, **notes})
    except Exception as e:
        return jsonify({'error': f'Note generation failed: {str(e)}'}), 500

@app.route('/visualize', methods=['POST'])
def visualize_document():
    try:
        data = request.get_json()
        filename = data.get('filename', '').strip()
        search_word = data.get('word', '').strip()
        if not filename:
            return jsonify({'error': 'No document loaded.'}), 400
        safe_filename = secure_filename(filename)
        text_file = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename + '.extracted.txt')
        if not os.path.exists(text_file):
            return jsonify({'error': 'Document session expired. Please re-upload.'}), 404
        with open(text_file, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
        words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
        word_freq = {}
        for w in words:
            if w not in STOPWORDS:
                word_freq[w] = word_freq.get(w, 0) + 1
        top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:12]
        word_positions = []
        if search_word:
            pattern = re.compile(r'\b' + re.escape(search_word) + r'\b', re.IGNORECASE)
            total_chars = len(text)
            for m in pattern.finditer(text):
                pct = round((m.start() / total_chars) * 100, 1)
                word_positions.append(pct)
        sentences = split_into_sentences(text)
        length_buckets = {'1-10': 0, '11-20': 0, '21-30': 0, '31-50': 0, '50+': 0}
        for s in sentences:
            wc = len(s.split())
            if wc <= 10:   length_buckets['1-10'] += 1
            elif wc <= 20: length_buckets['11-20'] += 1
            elif wc <= 30: length_buckets['21-30'] += 1
            elif wc <= 50: length_buckets['31-50'] += 1
            else:          length_buckets['50+'] += 1
        return jsonify({
            'top_words': [{'word': w, 'count': c} for w, c in top_words],
            'word_positions': word_positions,
            'sentence_lengths': length_buckets,
            'total_words': len(words),
            'total_sentences': len(sentences),
            'search_word': search_word,
            'search_count': len(word_positions),
        })
    except Exception as e:
        return jsonify({'error': f'Chart generation failed: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))