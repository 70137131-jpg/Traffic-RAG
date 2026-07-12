import os
import re
import threading
import uuid

from langchain_community.embeddings import SentenceTransformerEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder


DEFAULT_DOCUMENT = "traffic_laws.md"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
LLM_MODEL = "gemini-2.5-flash"


class RAGSystem:
    def __init__(self, default_document=DEFAULT_DOCUMENT):
        self.default_document = default_document
        self.current_doc_name = os.path.basename(default_document)
        self.vector_db = None
        self.bm25 = None
        self.docs = []
        self.reranker = None
        self.llm = None
        self.rag_chain = None
        self._lock = threading.RLock()

    def initialize(self, file_path=None):
        with self._lock:
            path = file_path or self.default_document
            print(f"Initializing RAG system with {path}...")

            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            except FileNotFoundError:
                print(f"Error: {path} not found.")
                return False
            except OSError as exc:
                print(f"Error reading file: {exc}")
                return False

            docs = self._split_document(path, raw_text)
            if not docs:
                print("Warning: no chunks were created from the document.")
                return False

            api_key = os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                print("Error: GOOGLE_API_KEY is not set.")
                return False

            try:
                embedding_function = SentenceTransformerEmbeddings(model_name=EMBEDDING_MODEL)
                vector_db = Chroma.from_documents(
                    documents=docs,
                    embedding=embedding_function,
                    collection_name=f"traffic_rag_{uuid.uuid4().hex}",
                )

                if self.reranker is None:
                    self.reranker = CrossEncoder(RERANKER_MODEL)

                if self.llm is None:
                    self.llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)

                self.docs = docs
                self.bm25 = BM25Okapi([self._tokenize(doc.page_content) for doc in docs])
                self.vector_db = vector_db
                self.current_doc_name = os.path.basename(path)
                self.rag_chain = self._build_chain()
            except Exception as exc:
                print(f"Error initializing RAG system: {exc}")
                return False

            print("RAG system initialized successfully.")
            return True

    def answer(self, query):
        with self._lock:
            if not self.rag_chain and not self.initialize():
                return {
                    "answer": (
                        "System not initialized. Set GOOGLE_API_KEY and make sure the "
                        "default document exists, then restart the app."
                    ),
                    "sources": [],
                }

            try:
                retrieved_docs = self._reranked_search(query)
                if not retrieved_docs:
                    return {
                        "answer": "I couldn't find any relevant information in the document.",
                        "sources": [],
                    }

                response = self.rag_chain.invoke(
                    {"context": self._format_docs(retrieved_docs), "question": query}
                )
            except Exception as exc:
                print(f"Error answering query: {exc}")
                return {
                    "answer": "I encountered an error while answering. Please try again.",
                    "sources": [],
                }

            return {
                "answer": response,
                "sources": [
                    {"content": doc.page_content, "metadata": doc.metadata}
                    for doc in retrieved_docs
                ],
            }

    def _split_document(self, file_path, raw_text):
        if file_path.lower().endswith(".md"):
            splitter = MarkdownHeaderTextSplitter(
                headers_to_split_on=[
                    ("#", "Title"),
                    ("##", "Section"),
                    ("###", "Subsection"),
                ]
            )
            return splitter.split_text(raw_text)

        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        return splitter.create_documents([raw_text])

    def _build_chain(self):
        template = """
You are a helpful assistant. You are currently answering questions based on the document: "{doc_name}".
Answer the question based ONLY on the following context.
If the answer is not in the context, say "I couldn't find that information in the uploaded document."

Context:
{context}

Question: {question}
"""
        prompt = ChatPromptTemplate.from_template(template)
        return prompt.partial(doc_name=self.current_doc_name) | self.llm | StrOutputParser()

    def _semantic_search(self, query, top_k=5):
        if not self.vector_db:
            return []
        return self.vector_db.similarity_search_with_score(query, k=top_k)

    def _bm25_search(self, query, top_k=5):
        if not self.bm25:
            return []
        return self.bm25.get_top_n(self._tokenize(query), self.docs, n=top_k)

    def _hybrid_search(self, query, top_k=10):
        if not self.bm25 or not self.vector_db:
            return []

        bm25_docs = self._bm25_search(query, top_k=top_k)
        semantic_docs = [doc for doc, _score in self._semantic_search(query, top_k=top_k)]

        combined = []
        seen = set()
        for i in range(top_k):
            if i < len(semantic_docs):
                self._append_unique(combined, seen, semantic_docs[i])
            if i < len(bm25_docs):
                self._append_unique(combined, seen, bm25_docs[i])
        return combined

    def _reranked_search(self, query, top_k=3):
        candidates = self._hybrid_search(query, top_k=10)
        if not candidates:
            return []

        pairs = [[query, doc.page_content] for doc in candidates]
        scores = self.reranker.predict(pairs)
        scored_candidates = sorted(zip(candidates, scores), key=lambda item: item[1], reverse=True)
        return [doc for doc, _score in scored_candidates[:top_k]]

    @staticmethod
    def _append_unique(combined, seen, doc):
        content = doc.page_content
        if content not in seen:
            combined.append(doc)
            seen.add(content)

    @staticmethod
    def _format_docs(docs):
        return "\n\n".join(doc.page_content for doc in docs)

    @staticmethod
    def _tokenize(text):
        return re.findall(r"\b\w+\b", text.lower())


rag_system = RAGSystem()


def process_new_document(file_path):
    """Public API to switch the active document."""
    return rag_system.initialize(file_path)


def get_answer(query):
    return rag_system.answer(query)
