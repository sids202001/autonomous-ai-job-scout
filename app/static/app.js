/**
 * AutoApply — Dashboard Application Logic
 * Handles all frontend interactions, API calls, and SPA routing.
 */

// ═══════════════════════════════════════════════════════════════════════════
// API Client
// ═══════════════════════════════════════════════════════════════════════════

const API = {
    /** @param {string} url @param {object} options */
    async request(url, options = {}) {
        try {
            const resp = await fetch(url, {
                headers: { 'Content-Type': 'application/json', ...options.headers },
                ...options,
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            return await resp.json();
        } catch (e) {
            console.error(`API Error [${url}]:`, e);
            throw e;
        }
    },
    get: (url) => API.request(url),
    post: (url, body) => API.request(url, { method: 'POST', body: JSON.stringify(body) }),
    put: (url, body) => API.request(url, { method: 'PUT', body: JSON.stringify(body) }),
    delete: (url) => API.request(url, { method: 'DELETE' }),
    upload: async (url, formData) => {
        const resp = await fetch(url, { method: 'POST', body: formData });
        if (!resp.ok) throw new Error('Upload failed');
        return await resp.json();
    },
};

// ═══════════════════════════════════════════════════════════════════════════
// Toast Notifications
// ═══════════════════════════════════════════════════════════════════════════

/** @param {string} message @param {'success'|'error'|'info'|'warning'} type */
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const icons = { success: '✅', error: '❌', info: 'ℹ️', warning: '⚠️' };
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-icon">${icons[type]}</span>
        <span class="toast-message">${message}</span>
    `;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('removing');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// ═══════════════════════════════════════════════════════════════════════════
// Loading Overlay
// ═══════════════════════════════════════════════════════════════════════════

function showLoading(text = 'Loading...') {
    document.getElementById('loaderText').textContent = text;
    document.getElementById('loadingOverlay').classList.add('active');
}

function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
}

// ═══════════════════════════════════════════════════════════════════════════
// SPA Router (Hash-based)
// ═══════════════════════════════════════════════════════════════════════════

function navigateTo(viewName) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));

    const view = document.getElementById(`view-${viewName}`);
    const link = document.querySelector(`.nav-link[data-view="${viewName}"]`);
    if (view) view.classList.add('active');
    if (link) link.classList.add('active');

    // Load view data
    switch (viewName) {
        case 'dashboard': loadDashboard(); break;
        case 'profile': loadProfile(); break;
        case 'jobs': loadJobs(); break;
        case 'applications': loadApplications(); break;
        case 'filters': loadFilters(); break;
    }
}

function initRouter() {
    window.addEventListener('hashchange', () => {
        const view = location.hash.replace('#', '') || 'dashboard';
        navigateTo(view);
    });

    document.querySelectorAll('.nav-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const view = link.dataset.view;
            location.hash = view;
        });
    });

    const initial = location.hash.replace('#', '') || 'dashboard';
    navigateTo(initial);
}

// ═══════════════════════════════════════════════════════════════════════════
// Dashboard View
// ═══════════════════════════════════════════════════════════════════════════

async function loadDashboard() {
    try {
        const stats = await API.get('/api/jobs/stats');
        document.getElementById('statTotalJobs').textContent = stats.total_jobs;
        document.getElementById('statApplied').textContent = stats.total_applied;
        document.getElementById('statInterviews').textContent = stats.total_interviews;
        document.getElementById('statSaved').textContent = stats.total_saved;
        document.getElementById('statOffers').textContent = stats.total_offers;

        renderRecentJobs(stats.recent_jobs || []);
    } catch (e) {
        // Stats might fail if no data yet, that's okay
        console.log('Dashboard load:', e.message);
    }
}

function renderRecentJobs(jobs) {
    const container = document.getElementById('recentJobsTable');
    if (!jobs.length) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🔍</div>
                <h4>No jobs found yet</h4>
                <p>Create a filter and run your first search!</p>
            </div>`;
        return;
    }

    container.innerHTML = `
        <table class="data-table">
            <thead>
                <tr>
                    <th>Job Title</th>
                    <th>Company</th>
                    <th>Location</th>
                    <th>Match</th>
                    <th>Source</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${jobs.map(j => `
                    <tr>
                        <td class="job-title-cell">${escHtml(j.title)}</td>
                        <td class="company-cell">${escHtml(j.company)}</td>
                        <td>${escHtml(j.location || '—')}</td>
                        <td>${renderScoreInline(j.match_score)}</td>
                        <td><span class="badge badge-${(j.source || '').toLowerCase().replace(/\s/g, '')}">${escHtml(j.source || '—')}</span></td>
                        <td>
                            <button class="btn btn-sm btn-secondary" onclick="saveJob(${j.id})">⭐ Save</button>
                            <button class="btn btn-sm btn-primary" onclick="applyToJob(${j.id})">📤 Apply</button>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Profile View
// ═══════════════════════════════════════════════════════════════════════════

async function loadProfile() {
    try {
        const profile = await API.get('/api/profile');
        fillProfileForm(profile);
    } catch (e) {
        // No profile yet
        console.log('No profile found, showing empty form');
    }

    loadSkills();
    loadExperiences();
    loadEducation();
}

function fillProfileForm(p) {
    document.getElementById('fullName').value = p.full_name || '';
    document.getElementById('email').value = p.email || '';
    document.getElementById('phone').value = p.phone || '';
    document.getElementById('location').value = p.location || '';
    document.getElementById('linkedin').value = p.linkedin_url || '';
    document.getElementById('github').value = p.github_url || '';
    document.getElementById('portfolio').value = p.portfolio_url || '';
    document.getElementById('workAuth').value = p.work_authorization || '';
    document.getElementById('salaryMin').value = p.desired_salary_min || '';
    document.getElementById('salaryMax').value = p.desired_salary_max || '';
    document.getElementById('yearsExp').value = p.years_experience || '';
    document.getElementById('summary').value = p.summary || '';
    if (p.resume_path) {
        document.getElementById('resumeStatus').textContent = `📎 Resume set: ${p.resume_path.split('/').pop()}`;
        document.getElementById('customResumePath').value = p.resume_path;
    }
}

async function saveProfile() {
    const data = {
        full_name: document.getElementById('fullName').value,
        email: document.getElementById('email').value,
        phone: document.getElementById('phone').value || null,
        location: document.getElementById('location').value || null,
        linkedin_url: document.getElementById('linkedin').value || null,
        github_url: document.getElementById('github').value || null,
        portfolio_url: document.getElementById('portfolio').value || null,
        work_authorization: document.getElementById('workAuth').value || null,
        desired_salary_min: parseInt(document.getElementById('salaryMin').value) || null,
        desired_salary_max: parseInt(document.getElementById('salaryMax').value) || null,
        years_experience: parseInt(document.getElementById('yearsExp').value) || null,
        summary: document.getElementById('summary').value || null,
    };

    if (!data.full_name || !data.email) {
        showToast('Name and email are required', 'error');
        return;
    }

    try {
        // Try update first, then create
        try {
            await API.put('/api/profile', data);
            showToast('Profile updated!', 'success');
        } catch {
            await API.post('/api/profile', data);
            showToast('Profile created!', 'success');
        }
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
    }
}

// ── Skills ──────────────────────────────────────────────────────────────────
async function loadSkills() {
    try {
        const skills = await API.get('/api/profile/skills');
        renderSkills(skills);
    } catch { renderSkills([]); }
}

function renderSkills(skills) {
    const container = document.getElementById('skillsTags');
    container.innerHTML = skills.map(s => `
        <span class="tag">
            ${escHtml(s.skill_name)}
            <span class="tag-level">${s.proficiency_level || ''}</span>
            <button class="tag-remove" onclick="deleteSkill(${s.id})">&times;</button>
        </span>
    `).join('');
}

async function addSkill() {
    const name = document.getElementById('newSkill').value.trim();
    const level = document.getElementById('skillLevel').value;
    if (!name) return;
    try {
        await API.post('/api/profile/skill', { skill_name: name, proficiency_level: level });
        document.getElementById('newSkill').value = '';
        loadSkills();
        showToast(`Skill "${name}" added`, 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function deleteSkill(id) {
    try {
        await API.delete(`/api/profile/skill/${id}`);
        loadSkills();
    } catch (e) { showToast(e.message, 'error'); }
}

// ── Experience ──────────────────────────────────────────────────────────────
async function loadExperiences() {
    try {
        const exps = await API.get('/api/profile/experience');
        renderExperiences(exps);
    } catch { renderExperiences([]); }
}

function renderExperiences(exps) {
    const container = document.getElementById('experienceList');
    if (!exps.length) {
        container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">No experience added yet.</p>';
        return;
    }
    container.innerHTML = exps.map(e => `
        <div class="entry-card">
            <div class="entry-info">
                <h4>${escHtml(e.title)} at ${escHtml(e.company)}</h4>
                <p>${escHtml(e.location || '')}</p>
                <p class="entry-dates">${e.start_date || ''} — ${e.is_current ? 'Present' : (e.end_date || '')}</p>
            </div>
            <button class="btn btn-danger btn-sm" onclick="deleteExperience(${e.id})">🗑</button>
        </div>
    `).join('');
}

async function saveExperience() {
    const data = {
        title: document.getElementById('expTitle').value,
        company: document.getElementById('expCompany').value,
        start_date: document.getElementById('expStart').value || null,
        end_date: document.getElementById('expEnd').value || null,
        location: document.getElementById('expLocation').value || null,
        is_current: document.getElementById('expCurrent').checked,
        description: document.getElementById('expDesc').value || null,
    };
    if (!data.title || !data.company) {
        showToast('Title and company are required', 'error');
        return;
    }
    try {
        await API.post('/api/profile/experience', data);
        closeModal('expModal');
        loadExperiences();
        showToast('Experience added!', 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function deleteExperience(id) {
    try {
        await API.delete(`/api/profile/experience/${id}`);
        loadExperiences();
    } catch (e) { showToast(e.message, 'error'); }
}

// ── Education ───────────────────────────────────────────────────────────────
async function loadEducation() {
    try {
        const edus = await API.get('/api/profile/education');
        renderEducation(edus);
    } catch { renderEducation([]); }
}

function renderEducation(edus) {
    const container = document.getElementById('educationList');
    if (!edus.length) {
        container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">No education added yet.</p>';
        return;
    }
    container.innerHTML = edus.map(e => `
        <div class="entry-card">
            <div class="entry-info">
                <h4>${escHtml(e.school)}</h4>
                <p>${escHtml(e.degree || '')} ${e.field_of_study ? `in ${escHtml(e.field_of_study)}` : ''}</p>
                <p class="entry-dates">${e.start_date || ''} — ${e.end_date || ''} ${e.gpa ? `• GPA: ${e.gpa}` : ''}</p>
            </div>
            <button class="btn btn-danger btn-sm" onclick="deleteEducation(${e.id})">🗑</button>
        </div>
    `).join('');
}

async function saveEducation() {
    const data = {
        school: document.getElementById('eduSchool').value,
        degree: document.getElementById('eduDegree').value || null,
        field_of_study: document.getElementById('eduField').value || null,
        start_date: document.getElementById('eduStart').value || null,
        end_date: document.getElementById('eduEnd').value || null,
        gpa: parseFloat(document.getElementById('eduGpa').value) || null,
    };
    if (!data.school) {
        showToast('School name is required', 'error');
        return;
    }
    try {
        await API.post('/api/profile/education', data);
        closeModal('eduModal');
        loadEducation();
        showToast('Education added!', 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function deleteEducation(id) {
    try {
        await API.delete(`/api/profile/education/${id}`);
        loadEducation();
    } catch (e) { showToast(e.message, 'error'); }
}

// ── Resume Upload ───────────────────────────────────────────────────────────
async function uploadResume(file) {
    const formData = new FormData();
    formData.append('file', file);
    try {
        const result = await API.upload('/api/profile/resume', formData);
        document.getElementById('resumeStatus').textContent = `📎 ${file.name} uploaded successfully`;
        showToast('Resume uploaded!', 'success');
    } catch (e) {
        showToast(`Upload failed: ${e.message}`, 'error');
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Jobs View
// ═══════════════════════════════════════════════════════════════════════════

let jobsPage = 1;

async function loadJobs() {
    const search = document.getElementById('jobSearchInput').value;
    const sortBy = document.getElementById('jobSortBy').value;
    const source = document.getElementById('jobSourceFilter').value;
    const minScore = document.getElementById('minScoreFilter').value;

    let url = `/api/jobs?page=${jobsPage}&per_page=20&sort_by=${sortBy}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;
    if (source) url += `&source=${encodeURIComponent(source)}`;
    if (minScore) url += `&min_score=${minScore}`;

    try {
        const data = await API.get(url);
        renderJobCards(data.jobs || []);
        renderPagination(data.total, data.page, data.per_page);
    } catch (e) {
        console.log('Jobs load error:', e.message);
    }
}

function renderJobCards(jobs) {
    const grid = document.getElementById('jobsGrid');
    if (!jobs.length) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🌐</div>
                <h4>No jobs loaded</h4>
                <p>Set up your filters and click "Run Search" to find matching jobs.</p>
            </div>`;
        return;
    }

    grid.innerHTML = jobs.map(j => {
        const scoreClass = j.match_score >= 70 ? 'high' : j.match_score >= 40 ? 'medium' : 'low';
        const remoteClass = (j.remote_type || '').toLowerCase();
        const salary = j.salary_min && j.salary_max
            ? `$${(j.salary_min/1000).toFixed(0)}k — $${(j.salary_max/1000).toFixed(0)}k`
            : j.salary_min ? `From $${(j.salary_min/1000).toFixed(0)}k` : '—';
        const desc = (j.description || '').substring(0, 200);

        return `
        <div class="job-card">
            <div class="job-card-header">
                <div>
                    <div class="job-card-title"><a href="${escHtml(j.url)}" target="_blank">${escHtml(j.title)}</a></div>
                    <div class="job-card-company">${escHtml(j.company)}</div>
                </div>
                <div class="match-score">
                    <div class="score-bar"><div class="score-fill ${scoreClass}" style="width: ${j.match_score}%"></div></div>
                    <span class="score-value ${scoreClass}">${Math.round(j.match_score)}</span>
                </div>
            </div>
            <div class="job-card-meta">
                <span class="job-meta-item">📍 ${escHtml(j.location || 'Unknown')}</span>
                <span class="badge badge-${remoteClass}">${escHtml(j.remote_type || 'Unknown')}</span>
                <span class="job-meta-item">💰 ${salary}</span>
                <span class="job-meta-item">🏷 ${escHtml(j.job_type || '')}</span>
                <span class="job-meta-item">📅 ${escHtml(j.posted_date || '')}</span>
            </div>
            <div class="job-card-desc">${escHtml(desc)}...</div>
            <div class="job-card-actions">
                <button class="btn btn-sm btn-secondary" onclick="saveJob(${j.id})">⭐ Save</button>
                <button class="btn btn-sm btn-primary" onclick="applyToJob(${j.id})">🚀 Auto-Fill & Apply</button>
                <button class="btn btn-sm btn-secondary" onclick="hideJob(${j.id})">🙈 Hide</button>
            </div>
        </div>`;
    }).join('');
}

function renderPagination(total, page, perPage) {
    const container = document.getElementById('jobsPagination');
    const totalPages = Math.ceil(total / perPage);
    if (totalPages <= 1) { container.innerHTML = ''; return; }

    let html = '';
    for (let i = 1; i <= totalPages; i++) {
        html += `<button class="page-btn ${i === page ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`;
    }
    container.innerHTML = html;
}

function goToPage(page) {
    jobsPage = page;
    loadJobs();
}

async function runJobSearch() {
    showLoading('Searching for jobs...');
    try {
        const result = await API.post('/api/jobs/search');
        hideLoading();
        showToast(result.message, 'success');
        loadJobs();
        loadDashboard();
    } catch (e) {
        hideLoading();
        showToast(`Search failed: ${e.message}`, 'error');
    }
}

async function saveJob(jobId) {
    try {
        await API.post('/api/applications/', { job_id: jobId, profile_id: 1 });
        showToast('Job saved!', 'success');
    } catch (e) { showToast(e.message, 'error'); }
}

async function applyToJob(jobId) {
    try {
        // First save as application
        let app;
        try {
            app = await API.post('/api/applications/', { job_id: jobId, profile_id: 1 });
        } catch {
            // Already exists, find it
            const apps = await API.get('/api/applications/');
            app = apps.find(a => a.job_id === jobId);
        }
        if (app) {
            showLoading('Opening browser and filling form...');
            const result = await API.post(`/api/applications/${app.id}/autofill`);
            hideLoading();
            showToast(result.message, 'info');
        }
    } catch (e) {
        hideLoading();
        showToast(`Auto-fill error: ${e.message}`, 'error');
    }
}

async function hideJob(jobId) {
    try {
        await API.put(`/api/jobs/${jobId}/hide`);
        loadJobs();
        showToast('Job hidden', 'info');
    } catch (e) { showToast(e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Applications View
// ═══════════════════════════════════════════════════════════════════════════

let currentStatusFilter = '';

async function loadApplications() {
    let url = '/api/applications';
    if (currentStatusFilter) url += `?status=${currentStatusFilter}`;
    try {
        const apps = await API.get(url);
        renderApplications(apps);
    } catch { renderApplications([]); }
}

function renderApplications(apps) {
    const container = document.getElementById('applicationsList');
    if (!apps.length) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📋</div>
                <h4>No applications${currentStatusFilter ? ` with status "${currentStatusFilter}"` : ''}</h4>
                <p>Save or apply to jobs to track them here.</p>
            </div>`;
        return;
    }

    container.innerHTML = apps.map(a => {
        const job = a.job || {};
        return `
        <div class="app-card">
            <div class="app-info">
                <h4>${escHtml(job.title || 'Unknown Job')}</h4>
                <p>${escHtml(job.company || '')} • ${escHtml(job.location || '')}</p>
                ${a.applied_at ? `<p style="color: var(--text-muted); font-size: 0.75rem;">Applied: ${new Date(a.applied_at).toLocaleDateString()}</p>` : ''}
            </div>
            <span class="status-badge status-${a.status}">${statusLabel(a.status)}</span>
            <div class="app-actions">
                <select onchange="updateAppStatus(${a.id}, this.value)">
                    ${['saved','applying','applied','interview','offer','rejected','withdrawn']
                        .map(s => `<option value="${s}" ${s === a.status ? 'selected' : ''}>${statusLabel(s)}</option>`)
                        .join('')}
                </select>
                <button class="btn btn-sm btn-primary" onclick="triggerAutofill(${a.id})">🤖 Auto-Fill</button>
                <button class="btn btn-sm btn-danger" onclick="deleteApplication(${a.id})">🗑</button>
            </div>
        </div>`;
    }).join('');
}

function statusLabel(status) {
    const labels = {
        saved: '⭐ Saved', applying: '🔄 Applying', applied: '✅ Applied',
        interview: '🎯 Interview', offer: '🏆 Offer', rejected: '❌ Rejected',
        withdrawn: '🚪 Withdrawn',
    };
    return labels[status] || status;
}

async function updateAppStatus(id, status) {
    try {
        await API.put(`/api/applications/${id}`, { status });
        showToast(`Status updated to ${status}`, 'success');
        loadApplications();
        loadDashboard();
    } catch (e) { showToast(e.message, 'error'); }
}

async function triggerAutofill(id) {
    showLoading('Opening browser and filling form...');
    try {
        const result = await API.post(`/api/applications/${id}/autofill`);
        hideLoading();
        showToast(result.message, 'info');
    } catch (e) {
        hideLoading();
        showToast(e.message, 'error');
    }
}

async function deleteApplication(id) {
    try {
        await API.delete(`/api/applications/${id}`);
        loadApplications();
        showToast('Application deleted', 'info');
    } catch (e) { showToast(e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Filters View
// ═══════════════════════════════════════════════════════════════════════════

let editingFilterId = null;

async function loadFilters() {
    try {
        const filters = await API.get('/api/filters');
        renderFilters(filters);
    } catch { renderFilters([]); }
}

function renderFilters(filters) {
    const grid = document.getElementById('filtersGrid');
    if (!filters.length) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">⚙️</div>
                <h4>No filters created</h4>
                <p>Create a filter to start searching for jobs automatically.</p>
            </div>`;
        return;
    }

    grid.innerHTML = filters.map(f => `
        <div class="filter-card">
            <div class="filter-card-header">
                <span class="filter-card-name">${escHtml(f.name)}</span>
                <label class="filter-toggle">
                    <input type="checkbox" ${f.is_active ? 'checked' : ''} onchange="toggleFilter(${f.id}, this.checked)">
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="filter-details">
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Keywords</span>
                    <span class="filter-detail-value">${escHtml(f.role_keywords || '—')}</span>
                </div>
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Location</span>
                    <span class="filter-detail-value">${escHtml(f.location || 'Any')}</span>
                </div>
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Remote</span>
                    <span class="filter-detail-value">${escHtml(f.remote_preference || 'Any')}</span>
                </div>
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Salary</span>
                    <span class="filter-detail-value">${f.min_salary ? `$${(f.min_salary/1000).toFixed(0)}k` : '—'} — ${f.max_salary ? `$${(f.max_salary/1000).toFixed(0)}k` : '—'}</span>
                </div>
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Level</span>
                    <span class="filter-detail-value">${escHtml(f.experience_level || 'Any')}</span>
                </div>
                <div class="filter-detail-row">
                    <span class="filter-detail-label">Type</span>
                    <span class="filter-detail-value">${escHtml(f.job_type || 'Any')}</span>
                </div>
            </div>
            <div class="filter-card-actions">
                <button class="btn btn-sm btn-secondary" onclick="editFilter(${f.id})">✏️ Edit</button>
                <button class="btn btn-sm btn-danger" onclick="deleteFilter(${f.id})">🗑 Delete</button>
            </div>
        </div>
    `).join('');
}

async function toggleFilter(id, active) {
    try {
        await API.put(`/api/filters/${id}/${active ? 'activate' : 'deactivate'}`);
        showToast(`Filter ${active ? 'activated' : 'deactivated'}`, 'info');
    } catch (e) { showToast(e.message, 'error'); }
}

function openFilterModal(filter = null) {
    editingFilterId = filter ? filter.id : null;
    document.getElementById('filterModalTitle').textContent = filter ? 'Edit Filter' : 'Create Filter';

    document.getElementById('filterName').value = filter?.name || '';
    document.getElementById('filterKeywords').value = filter?.role_keywords || '';
    document.getElementById('filterLocation').value = filter?.location || '';
    document.getElementById('filterRemote').value = filter?.remote_preference || 'Any';
    document.getElementById('filterMinSalary').value = filter?.min_salary || '';
    document.getElementById('filterMaxSalary').value = filter?.max_salary || '';
    document.getElementById('filterExpLevel').value = filter?.experience_level || '';
    document.getElementById('filterJobType').value = filter?.job_type || 'Full-time';
    document.getElementById('filterExcluded').value = filter?.excluded_companies || '';

    document.getElementById('filterModal').classList.add('active');
}

async function editFilter(id) {
    try {
        const filter = await API.get(`/api/filters/${id}`);
        openFilterModal(filter);
    } catch (e) { showToast(e.message, 'error'); }
}

async function saveFilter() {
    const data = {
        name: document.getElementById('filterName').value || 'Untitled Filter',
        role_keywords: document.getElementById('filterKeywords').value || null,
        location: document.getElementById('filterLocation').value || null,
        remote_preference: document.getElementById('filterRemote').value,
        min_salary: parseInt(document.getElementById('filterMinSalary').value) || null,
        max_salary: parseInt(document.getElementById('filterMaxSalary').value) || null,
        experience_level: document.getElementById('filterExpLevel').value || null,
        job_type: document.getElementById('filterJobType').value,
        excluded_companies: document.getElementById('filterExcluded').value || null,
    };

    try {
        if (editingFilterId) {
            await API.put(`/api/filters/${editingFilterId}`, data);
            showToast('Filter updated!', 'success');
        } else {
            await API.post('/api/filters', data);
            showToast('Filter created!', 'success');
        }
        closeModal('filterModal');
        loadFilters();
    } catch (e) { showToast(e.message, 'error'); }
}

async function deleteFilter(id) {
    try {
        await API.delete(`/api/filters/${id}`);
        loadFilters();
        showToast('Filter deleted', 'info');
    } catch (e) { showToast(e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════════════
// Modal Helpers
// ═══════════════════════════════════════════════════════════════════════════

function openModal(id) { document.getElementById(id).classList.add('active'); }
function closeModal(id) { document.getElementById(id).classList.remove('active'); }

// ═══════════════════════════════════════════════════════════════════════════
// Score Rendering
// ═══════════════════════════════════════════════════════════════════════════

function renderScoreInline(score) {
    const cls = score >= 70 ? 'high' : score >= 40 ? 'medium' : 'low';
    return `
        <div class="match-score">
            <div class="score-bar"><div class="score-fill ${cls}" style="width: ${score}%"></div></div>
            <span class="score-value ${cls}">${Math.round(score)}</span>
        </div>`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════════════════

function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════════════
// Event Listeners — Init
// ═══════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    initRouter();

    // Dashboard search button
    document.getElementById('dashSearchBtn').addEventListener('click', runJobSearch);

    // Profile
    document.getElementById('saveProfileBtn').addEventListener('click', saveProfile);
    document.getElementById('addSkillBtn').addEventListener('click', addSkill);
    document.getElementById('newSkill').addEventListener('keypress', e => { if (e.key === 'Enter') addSkill(); });

    // Resume upload
    const resumeFileInput = document.getElementById('resumeFile');
    const uploadArea = document.getElementById('resumeUpload');
    resumeFileInput.addEventListener('change', e => { if (e.target.files[0]) uploadResume(e.target.files[0]); });
    uploadArea.addEventListener('click', () => resumeFileInput.click());
    uploadArea.addEventListener('dragover', e => { e.preventDefault(); uploadArea.classList.add('drag-over'); });
    uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('drag-over'));
    uploadArea.addEventListener('drop', e => {
        e.preventDefault();
        uploadArea.classList.remove('drag-over');
        if (e.dataTransfer.files[0]) uploadResume(e.dataTransfer.files[0]);
    });

    // Experience modal
    document.getElementById('addExpBtn').addEventListener('click', () => openModal('expModal'));
    document.getElementById('closeExpModal').addEventListener('click', () => closeModal('expModal'));
    document.getElementById('cancelExpBtn').addEventListener('click', () => closeModal('expModal'));
    document.getElementById('saveExpBtn').addEventListener('click', saveExperience);

    // Education modal
    document.getElementById('addEduBtn').addEventListener('click', () => openModal('eduModal'));
    document.getElementById('closeEduModal').addEventListener('click', () => closeModal('eduModal'));
    document.getElementById('cancelEduBtn').addEventListener('click', () => closeModal('eduModal'));
    document.getElementById('saveEduBtn').addEventListener('click', saveEducation);

    // Jobs
    document.getElementById('runSearchBtn').addEventListener('click', runJobSearch);
    document.getElementById('jobSearchInput').addEventListener('input', debounce(loadJobs, 400));
    document.getElementById('jobSortBy').addEventListener('change', loadJobs);
    document.getElementById('jobSourceFilter').addEventListener('change', loadJobs);
    document.getElementById('minScoreFilter').addEventListener('change', loadJobs);

    // Applications status tabs
    document.querySelectorAll('#statusTabs .tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('#statusTabs .tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentStatusFilter = tab.dataset.status;
            loadApplications();
        });
    });

    // Filters
    document.getElementById('addFilterBtn').addEventListener('click', () => openFilterModal());
    document.getElementById('closeFilterModal').addEventListener('click', () => closeModal('filterModal'));
    document.getElementById('cancelFilterBtn').addEventListener('click', () => closeModal('filterModal'));
    document.getElementById('saveFilterBtn').addEventListener('click', saveFilter);

    // Resume path handler
    const btnParsePath = document.getElementById('btnParseResumePath');
    if (btnParsePath) {
        btnParsePath.addEventListener('click', parseResumePath);
    }
});

async function parseResumePath() {
    const pathInput = document.getElementById('customResumePath');
    const path = pathInput ? pathInput.value.trim() : '';
    if (!path) {
        showToast('Please enter a valid resume file path', 'error');
        return;
    }

    showLoading('Parsing resume & extracting skills...');
    try {
        const res = await API.post('/api/profile/resume-path', { path: path });
        hideLoading();
        showToast(res.message, 'success');
        
        // Reload profile data & skills
        loadProfile();

        // Run search to filter jobs based on newly extracted skills
        showLoading('Finding & scoring matching jobs...');
        await API.post('/api/jobs/search');
        hideLoading();

        showToast(`Jobs filtered & matched based on ${res.extracted_skills.length || 0} extracted skills!`, 'success');
        
        // Navigate to jobs view
        location.hash = 'jobs';
    } catch (e) {
        hideLoading();
        showToast(e.message, 'error');
    }
}

/** @param {Function} fn @param {number} delay */
function debounce(fn, delay) {
    let timer;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
    };
}
