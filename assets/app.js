/* FormatFlip Pro — browser interactions and server conversion */
const MAX_FILE_BYTES = 200 * 1024 * 1024;
const fileInput = document.getElementById('fileIn');
const dropZone = document.getElementById('dropZone');
const selectedFile = document.getElementById('selectedFile');
const selectedFileName = document.getElementById('selectedFileName');
const progressWrap = document.getElementById('progressWrap');
const progressFill = document.getElementById('progressFill');
const statusText = document.getElementById('statusText');
const resultArea = document.getElementById('resultArea');
const resultMeta = document.getElementById('resultMeta');
const downloadBtn = document.getElementById('downloadBtn');
const convertBtn = document.getElementById('btnConvert');

function formatSize(bytes) {
  return bytes < 1048576 ? `${(bytes / 1024).toFixed(0)} КБ` : `${(bytes / 1048576).toFixed(1)} МБ`;
}

function showSelectedFile(file) {
  if (!file) return;
  selectedFileName.textContent = `${file.name} · ${formatSize(file.size)}`;
  selectedFile.hidden = false;
  resultArea.hidden = true;
  progressWrap.hidden = true;
  statusText.textContent = '';
  progressFill.style.width = '0%';
}

fileInput.addEventListener('change', () => showSelectedFile(fileInput.files[0]));
document.getElementById('removeFile').addEventListener('click', () => {
  fileInput.value = '';
  selectedFile.hidden = true;
  resultArea.hidden = true;
  progressWrap.hidden = true;
  statusText.textContent = '';
});

dropZone.addEventListener('dragover', (event) => {
  event.preventDefault();
  dropZone.classList.add('drag-over');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropZone.classList.remove('drag-over');
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  showSelectedFile(file);
});

async function runConvertServer() {
  const file = fileInput.files && fileInput.files[0];
  const target = document.getElementById('targetType').value;
  progressWrap.hidden = false;
  resultArea.hidden = true;
  if (!file) {
    statusText.textContent = 'Сначала выберите файл.';
    progressFill.style.width = '0%';
    return;
  }
  if (file.size > MAX_FILE_BYTES) {
    statusText.textContent = 'Файл больше лимита 200 МБ.';
    progressFill.style.width = '0%';
    return;
  }
  convertBtn.disabled = true;
  convertBtn.querySelector('span:first-child').textContent = 'Конвертирую…';
  progressFill.style.width = '15%';
  statusText.textContent = 'Загружаю файл и запускаю конвертацию…';
  try {
    const form = new FormData();
    form.append('file', file);
    form.append('output_format', target);
    const response = await fetch('/api/convert', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось конвертировать файл.');
    progressFill.style.width = '100%';
    statusText.textContent = 'Готово — можно скачивать.';
    downloadBtn.href = data.download_url;
    downloadBtn.download = data.filename;
    resultMeta.textContent = `Формат .${target.toUpperCase()} · исходник ${formatSize(file.size)}`;
    resultArea.hidden = false;
  } catch (error) {
    progressFill.style.width = '0%';
    statusText.textContent = error.message || 'Ошибка сервера. Попробуйте ещё раз.';
  } finally {
    convertBtn.disabled = false;
    convertBtn.querySelector('span:first-child').textContent = 'Конвертировать';
  }
}
window.runConvertServer = runConvertServer;
