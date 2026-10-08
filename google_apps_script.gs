// --- CareerForge Google Sheets & Docs Bridge ---
function doGet(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data = sheet.getDataRange().getValues();
  if (data.length <= 1) {
    return ContentService.createTextOutput(JSON.stringify({ status: "success", data: [] }))
      .setMimeType(ContentService.MimeType.JSON);
  }
  var headers = data[0];
  var rows = [];
  for (var i = 1; i < data.length; i++) {
    var item = {};
    for (var j = 0; j < headers.length; j++) {
      item[headers[j]] = data[i][j];
    }
    rows.push(item);
  }
  return ContentService.createTextOutput(JSON.stringify({ status: "success", data: rows }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  var payload = JSON.parse(e.postData.contents);
  var action = payload.action;

  // ACTION 1: CREATE GOOGLE DOC PREP TEMPLATE
  if (action === "createDoc") {
    var appData = payload.data;
    var doc = DocumentApp.create(appData.company + " - " + appData.jobTitle + " Interview Prep");
    var body = doc.getBody();
    body.insertParagraph(0, appData.company + " Interview Prep Kit")
        .setHeading(DocumentApp.ParagraphHeading.HEADING_1);
    body.appendParagraph("Job Title: " + appData.jobTitle);
    body.appendParagraph("Pipeline Stage: " + appData.status);
    body.appendParagraph("Compensation: " + (appData.salary || "N/A"));
    body.appendParagraph("Work Mode: " + (appData.workType || "N/A"));
    body.appendParagraph("Posting Link: " + (appData.url || "N/A"));
    body.appendParagraph("Company Website: " + (appData.companyWebsite || "N/A"));
    body.appendParagraph("\n--- 1. COMPANY RESEARCH & VALUES ---\n");
    body.appendParagraph("--- 2. ROLE REQUIREMENTS & SYSTEM FIT ---\n");
    body.appendParagraph("--- 3. QUESTIONS FOR THE INTERVIEWER ---\n");
    body.appendParagraph("--- 4. POST-INTERVIEW DEBRIEF & NEXT STEPS ---\n");
    body.appendParagraph("Notes: " + (appData.notes || ""));
    doc.saveAndClose();

    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      docUrl: doc.getUrl(),
      docId: doc.getId()
    })).setMimeType(ContentService.MimeType.JSON);
  }

  // ACTION 2: BULK SAVE / SYNC TO SHEET
  if (action === "syncAll") {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getActiveSheet();
    sheet.clearContents();

    var headers = ["id", "jobTitle", "company", "status", "dateApplied", "location", "workType", "salary", "source", "url", "contact", "nextStep", "notes", "companyWebsite", "jobDescription", "linkedInJobId", "publishedAt", "applyUrl", "resumeUrl", "coverLetterUrl"];
    sheet.appendRow(headers);

    var rows = payload.data.map(function(app) {
      return headers.map(function(key) { return app[key] || ""; });
    });

    if (rows.length > 0) {
      sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
    }

    return ContentService.createTextOutput(JSON.stringify({ status: "success", count: rows.length }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  return ContentService.createTextOutput(JSON.stringify({ status: "unknown action" }))
    .setMimeType(ContentService.MimeType.JSON);
}
