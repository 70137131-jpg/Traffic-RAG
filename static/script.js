document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const chatHistory = document.getElementById('chat-history');
    const sourcesContainer = document.getElementById('sources-container');
    const typingIndicator = document.getElementById('typing-indicator');

    const botAvatarSVG = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-bot"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>`;
    const userAvatarSVG = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-user"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;

    function scrollToBottom() {
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    function showTyping() {
        chatHistory.appendChild(typingIndicator);
        typingIndicator.style.display = 'flex';
        scrollToBottom();
    }

    function hideTyping() {
        typingIndicator.style.display = 'none';
    }

    function addMessage(text, isUser) {
        const div = document.createElement('div');
        div.className = `message ${isUser ? 'user-message' : 'bot-message'}`;

        const avatar = document.createElement('div');
        avatar.className = 'message-avatar';
        avatar.innerHTML = isUser ? userAvatarSVG : botAvatarSVG;

        const content = document.createElement('div');
        content.className = 'message-content';
        content.textContent = text;

        div.appendChild(avatar);
        div.appendChild(content);

        if (typingIndicator.parentNode === chatHistory && typingIndicator.style.display !== 'none') {
            chatHistory.insertBefore(div, typingIndicator);
        } else {
            chatHistory.appendChild(div);
        }

        scrollToBottom();
    }

    function updateSources(sources) {
        sourcesContainer.innerHTML = '';
        if (!sources || sources.length === 0) {
            sourcesContainer.innerHTML = `
                <div class="empty-state">
                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-book-open"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
                    <p>No specific laws cited for this query.</p>
                </div>`;
            return;
        }

        sources.forEach((source, index) => {
            const card = document.createElement('div');
            card.className = 'source-card';
            card.style.animationDelay = `${index * 0.1}s`;

            const meta = document.createElement('div');
            meta.className = 'source-meta';
            const metaText = Object.entries(source.metadata)
                .map(([k, v]) => `${k}: ${v}`)
                .join(' | ');
            meta.textContent = metaText;

            const content = document.createElement('div');
            content.textContent = source.content;

            card.appendChild(meta);
            card.appendChild(content);
            sourcesContainer.appendChild(card);
        });
    }

    async function sendMessage() {
        const text = input.value.trim();
        if (!text) return;

        addMessage(text, true);
        input.value = '';

        showTyping();

        try {
            const response = await fetch('/ask', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ query: text }),
            });

            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Request failed");
            }

            hideTyping();
            addMessage(data.answer, false);
            updateSources(data.sources);

        } catch (error) {
            console.error('Error:', error);
            hideTyping();
            addMessage("Sorry, I encountered an error connecting to the server.", false);
        }
    }

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') sendMessage();
    });

    // File Upload Handling
    const fileInput = document.getElementById('file-upload');
    const docLabel = document.getElementById('current-doc-label');

    function updateDocLabel(text, statusColor = null) {
        let span = docLabel.querySelector('.truncate-text');
        if (!span) {
            // Re-create the span if it was somehow removed
            span = document.createElement('span');
            span.className = 'truncate-text';
            docLabel.appendChild(span);
        }
        span.textContent = text;
        if (statusColor) {
            span.style.color = statusColor;
        } else {
            span.style.color = 'var(--text-primary)';
        }
    }

    if (fileInput) {
        fileInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            const formData = new FormData();
            formData.append('file', file);

            updateDocLabel("Uploading & Indexing...", "#ECC94B"); // Yellow

            try {
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });

                const data = await response.json();

                if (response.ok) {
                    updateDocLabel(data.filename, "#48BB78"); // Green
                    addMessage(`System: Successfully switched knowledge base to "${data.filename}".`, false);
                } else {
                    throw new Error(data.error || "Upload failed");
                }
            } catch (error) {
                console.error('Upload Error:', error);
                updateDocLabel("Error Uploading", "#F56565"); // Red
                addMessage(`System: Error uploading file. ${error.message}`, false);
            }

            fileInput.value = '';
        });
    }
});
