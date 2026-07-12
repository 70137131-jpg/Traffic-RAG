document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const chatHistory = document.getElementById('chat-history');
    const sourcesContainer = document.getElementById('sources-container');
    const typingIndicator = document.getElementById('typing-indicator');

    function scrollToBottom() {
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    function showTyping() {
        // Move typing indicator to the end of chat history
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
        div.textContent = text;

        // Insert before typing indicator if it exists
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
            sourcesContainer.innerHTML = '<p class="placeholder-text">No specific laws cited.</p>';
            return;
        }

        sources.forEach((source, index) => {
            const card = document.createElement('div');
            card.className = 'source-card';
            card.style.animationDelay = `${index * 0.1}s`; // Staggered animation

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

        showTyping(); // Show animation

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

            hideTyping(); // Hide animation
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

    if (fileInput) {
        fileInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file) return;

            const formData = new FormData();
            formData.append('file', file);

            // Show loading state
            docLabel.textContent = "Uploading & Indexing...";
            docLabel.style.color = "#f59e0b"; // Warning color

            try {
                const response = await fetch('/upload', {
                    method: 'POST',
                    body: formData
                });

                const data = await response.json();

                if (response.ok) {
                    docLabel.textContent = `Active: ${data.filename}`;
                    docLabel.style.color = "#00ff88"; // Success color
                    addMessage(`System: Successfully switched knowledge base to "${data.filename}".`, false);
                } else {
                    throw new Error(data.error || "Upload failed");
                }
            } catch (error) {
                console.error('Upload Error:', error);
                docLabel.textContent = "Error Uploading";
                docLabel.style.color = "#ef4444"; // Error color
                addMessage(`System: Error uploading file. ${error.message}`, false);
            }

            // Reset input
            fileInput.value = '';
        });
    }
});
