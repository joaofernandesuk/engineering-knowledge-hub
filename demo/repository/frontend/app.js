export async function loadReports() {
  const response = await fetch('/api/reports');
  return response.json();
}
