document.addEventListener('DOMContentLoaded', () => {
    const generateBtn = document.getElementById('generateBtn');
    const cancelBtn = document.getElementById('cancelBtn');
    const promptArea = document.getElementById('prompt');
    const languageSelect = document.getElementById('language');
    const outputDiv = document.getElementById('output');
    const progressDiv = document.getElementById('progress');
    const progressBar = document.getElementById('progressBar');
    const progressMessage = document.getElementById('progressMessage');
    const historyList = document.getElementById('historyList');
    const toastContainer = document.getElementById('toastContainer');

    let currentProject = null;
    let currentPrompt = null;
    let currentLanguage = null;

    // Load history
    function loadHistory() {
        fetch('/history.json')
            .then(res => res.json())
            .then(history => {
                historyList.innerHTML = '';
                history.forEach((item, index) => {
                    const li = document.createElement('li');
                    li.dataset.index = index;
                    li.innerHTML = `<span>${item.prompt.substring(0, 30)}...</span><span class="rating-icon">${getRatingIcon(item.rating)}</span>`;
                    historyList.appendChild(li);
                });
            });
    }
    loadHistory();

    // Event delegation for history clicks
    historyList.addEventListener('click', (e) => {
        const li = e.target.closest('li');
        if (!li) return;
        const index = li.dataset.index;
        fetch('/history.json')
            .then(res => res.json())
            .then(history => {
                const item = history[index];
                if (item) {
                    currentPrompt = item.prompt;
                    currentLanguage = item.language;
                    currentProject = item.project;
                    displayProject(item.project);
                    // Enable push button (if implemented)
                }
            });
    });

    function getRatingIcon(rating) {
        if (rating === 1) return '👍';
        if (rating === -1) return '👎';
        return '';
    }

    // Generate
    generateBtn.addEventListener('click', async () => {
        const prompt = promptArea.value.trim();
        if (!prompt) {
            showToast('Please enter a prompt', 'error');
            return;
        }
        currentPrompt = prompt;
        currentLanguage = languageSelect.value;
        generateBtn.disabled = true;
        cancelBtn.style.display = 'inline-block';
        progressDiv.style.display = 'block';
        outputDiv.innerHTML = '';

        // First check for template
        const formData = new FormData();
        formData.append('prompt', prompt);
        formData.append('language', currentLanguage);
        const templateRes = await fetch('/generate', { method: 'POST', body: formData });
        const templateData = await templateRes.json();
        if (templateData.template) {
            // Ask user
            const use = confirm(`Template found: ${templateData.match}. Use it? (Yes/Modify/No)`);
            if (use) {
                // Use template directly
                currentProject = templateData.template;
                displayProject(currentProject);
                generateBtn.disabled = false;
                cancelBtn.style.display = 'none';
                progressDiv.style.display = 'none';
                return;
            } else {
                // Modify or no: proceed with generation
            }
        }

        // Start SSE for progress (simplified)
        const eventSource = new EventSource('/progress');
        eventSource.onmessage = (event) => {
            const data = JSON.parse(event.data);
            progressBar.style.width = data.step + '%';
            progressMessage.textContent = data.message;
            if (data.step === 100) {
                eventSource.close();
            }
        };

        // Generate full project
        const formDataFull = new FormData();
        formDataFull.append('prompt', prompt);
        formDataFull.append('language', currentLanguage);
        try {
            const res = await fetch('/generate_full', { method: 'POST', body: formDataFull });
            const project = await res.json();
            if (project.cancelled) {
                showToast('Generation cancelled', 'error');
            } else {
                currentProject = project;
                displayProject(project);
                showToast('Project generated successfully', 'success');
                loadHistory();
            }
        } catch (err) {
            showToast('Generation failed', 'error');
        } finally {
            generateBtn.disabled = false;
            cancelBtn.style.display = 'none';
            progressDiv.style.display = 'none';
            eventSource.close();
        }
    });

    cancelBtn.addEventListener('click', async () => {
        await fetch('/cancel', { method: 'POST' });
        cancelBtn.style.display = 'none';
    });

    function displayProject(project) {
        let html = `<h2>${project.summary}</h2><hr>`;
        project.files.forEach(file => {
            html += `<h4>${file.path}</h4><pre><code>${escapeHtml(file.content)}</code></pre>`;
        });
        // Add rating buttons
        html += `<div class="rating">
            <button onclick="rateProject(1)">👍</button>
            <button onclick="rateProject(-1)">👎</button>
        </div>`;
        // Push button
        html += `<button id="pushBtn">Push to GitHub</button>`;
        outputDiv.innerHTML = html;

        // Push functionality
        document.getElementById('pushBtn').addEventListener('click', pushToGitHub);
    }

    window.rateProject = async (rating) => {
        const formData = new FormData();
        formData.append('prompt', currentPrompt);
        formData.append('rating', rating);
        await fetch('/rate', { method: 'POST', body: formData });
        loadHistory();
    };

    async function pushToGitHub() {
        const repoName = prompt('Enter repository name:', currentPrompt.replace(/\s+/g, '-').toLowerCase());
        if (!repoName) return;
        const privateRepo = confirm('Private repository?');
        const token = prompt('Enter GitHub personal access token:');
        if (!token) return;
        const formData = new FormData();
        formData.append('repo_name', repoName);
        formData.append('private', privateRepo);
        formData.append('github_token', token);
        try {
            const res = await fetch('/push', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                showToast(`Pushed successfully: <a href="${data.url}" target="_blank">${data.url}</a>`, 'success');
            } else {
                showToast('Push failed', 'error');
            }
        } catch (err) {
            showToast('Push error', 'error');
        }
    }

    function showToast(message, type) {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = message;
        toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
});
