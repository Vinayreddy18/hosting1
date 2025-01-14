import requests
import os
import openai
from github import Github
import re
import hashlib
import json

ai_provider = os.getenv('AI_PROVIDER')
openrouter_api_key = os.getenv('OPENROUTER_API_KEY')
openrouter_model_id = os.getenv('OPENROUTER_MODEL_ID', 'anthropic/claude-3.5-sonnet:beta')
openai.api_key = os.getenv('OPENAI_API_KEY')
MAX_TOKENS = os.getenv('MAX_TOKENS')
github_token = os.getenv('GITHUB_TOKEN')
repo_name = os.getenv('GITHUB_REPOSITORY')
pr_number = os.getenv('PR_NUMBER')
event_name = os.getenv('EVENT_NAME')
openai_model = os.getenv('OPENAI_MODEL', 'text-davinci-002')

g = Github(github_token)
repo = g.get_repo(repo_name)
pr = repo.get_pull(int(pr_number))

# Dictionary to store file hashes
file_hashes = {}

def call_ai_api(messages):
    if ai_provider == 'openai':
        return call_openai_api(messages)
    elif ai_provider == 'openrouter':
        return call_openrouter_api(messages)
    else:
        raise ValueError(f"Unsupported AI provider: {ai_provider}")

def call_openai_api(messages):
    response = openai.ChatCompletion.create(
        model=openai_model,
        messages=messages,
        max_tokens=1000
    )
    return response.choices[0].message['content'].strip()

def call_openrouter_api(messages):
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {openrouter_api_key}",
            "Content-Type": "application/json"
        },
        data=json.dumps({
            "model": openrouter_model_id,
            "messages": messages,
            "max_tokens": 1000
        })
    )
    response_json = response.json()
    if 'choices' in response_json:
        return response_json['choices'][0]['message']['content'].strip()
    elif 'error' in response_json:
        error_message = response_json['error'].get('message', 'Unknown error')
        full_error = json.dumps(response_json['error'], indent=4)
        raise ValueError(f"OpenRouter API error: {error_message}\nError details: {full_error}")
    else:
        raise ValueError("Unknown error while processing OpenRouter API response")

def review_pr():
    excluded_extensions = ('.exe', '.dll', '.so', '.dylib', '.bin')

    if event_name == 'pull_request':
        files = pr.get_files()
        all_file_hashes = get_all_file_hashes_from_comments(pr)
        file_hashes_to_update = {}

        for file in files:
            if file.status == 'removed':
                print(f"File deleted! 🚨 {file.filename}")
                current_file_hash = 'removed'
                previous_file_hash = all_file_hashes.get(file.filename)

                if previous_file_hash != current_file_hash:
                    review_comment = f"**🚨️ The file '{file.filename}' was deleted!** 🚨️\nPlease check if this change affects other parts!"
                    pr.create_issue_comment(review_comment)
                    file_hashes_to_update[file.filename] = current_file_hash  # Update the deleted file's status

                continue  # Skip code review for deleted files

            print(f"Reviewing file: {file.filename}")
            if not file.filename.endswith(excluded_extensions):
                current_file_content = file.patch
                current_file_hash = calculate_file_hash(current_file_content)
                previous_file_hash = all_file_hashes.get(file.filename)

                if previous_file_hash is None or current_file_hash != previous_file_hash:
                    print(f"Reviewing file: {file.filename}")
                    conversation_history = get_conversation_history(pr, file.filename)
                    try:
                        previous_diff = get_all_previous_diffs(pr, file.filename)
                    except Exception as e:
                        print(f"Error fetching previous diff: {str(e)}")
                        previous_diff = ""

                    review_result = review_code(file.patch, previous_diff, conversation_history)
                    pr.create_issue_comment(f"AI Review for {file.filename}:\n\n{review_result}")

                    file_hashes_to_update[file.filename] = current_file_hash
                else:
                    print(f"File {file.filename} has not been modified. Skipping review!")
            else:
                print(f"Unsupported file type: {file.filename}")

        if file_hashes_to_update:
            update_all_file_hashes_in_comment(pr, file_hashes_to_update)

    elif event_name == 'issue_comment':
        comment_id = os.getenv('COMMENT_ID')
        if comment_id:
            comment = repo.get_issue(int(pr_number)).get_comment(int(comment_id))
            if comment.user.login != 'github-actions[bot]':
                files = pr.get_files()
                file_content = "\n".join([f"File: {file.filename}\n{file.patch}" for file in files])
                conversation_history = get_conversation_history(pr)

                try:
                    response = respond_to_comment(comment.body, file_content, conversation_history)
                    pr.create_issue_comment(response)
                except Exception as e:
                    pr.create_issue_comment(f"Oops, an error occurred while generating the response 😅: {str(e)}")
        else:
            print("COMMENT_ID is not set! 😅")

    else:
        print(f"Unsupported event type: {event_name}")

# Function to calculate file hash
def calculate_file_hash(file_content):
    return hashlib.sha256(file_content.encode('utf-8')).hexdigest()

def get_conversation_history(pr, file_path=None):
    comments = pr.get_issue_comments()
    conversation = []
    for comment in comments:
        if file_path is None or file_path in comment.body:
            if comment.user.login == 'github-actions[bot]':
                # AI's comment
                ai_review = re.search(r'AI Review for.*?:\n\n(.*?)(?=\n\nConclusion\s*:\s*)', comment.body, re.DOTALL)
                if ai_review:
                    conversation.append({"role": "assistant", "content": ai_review.group(1).strip()})
            else:
                # User's comment
                conversation.append({"role": "user", "content": comment.body})
    return conversation

def get_previous_diff(pr, file_path):
    commits = list(pr.get_commits())
    if len(commits) > 1:
        previous_commit = commits[-2]
        for file in previous_commit.files:
            if file.filename == file_path:
                return file.patch
    return ""

def get_all_previous_diffs(pr, file_path):
    all_diffs = []
    commits = list(pr.get_commits())
    for commit in commits[:-1]:  # Exclude current commit
        for file in commit.files:
            if file.filename == file_path:
                all_diffs.append(f"Commit {commit.sha[:7]}:\n{file.patch}")
    return "\n\n".join(all_diffs)

def review_code(current_diff, previous_diff, conversation_history):
    messages = [
        {"role": "system", "content": "You are a helpful and informative code reviewer. Consider the previous conversation history and review the current code changes. First find something to praise, then focus on these three main aspects: **1. Verify changes and functionality ✅** **2. Code quality (bugs, readability, maintainability) 🧐** **3. Performance and optimization 🚀**. For code quality/readability, only suggest method documentation comments for complex methods. If there are areas for improvement, create a '**🎯 Suggestions for Improvement**' section with specific code examples. End the review with praise, and if changes are needed before merging, clearly indicate the file locations that need modification and request additional commits for review. If suggestions for improvement are present but deemed non-essential, it’s okay to merge without additional commits. Offer to answer any questions through comments. Use many emojis and respond in Korean with a casual, friendly tone."},
    ]

    # Add conversation history
    messages.extend(conversation_history)

    # Add new user message at the end
    messages.append({"role": "user", "content": f"Previous diff:\n{previous_diff}\n\nCurrent diff:\n{current_diff}\n\nCompare these two diffs, focusing on the most recent (top) item from the previous diff and thoroughly review all changes in the current diff!"})

    review = call_ai_api(messages)

    merge_decision = call_ai_api([
        {"role": "system", "content": "Based on the review content, make a merge decision. Respond only with either 'It seems safe to merge 💯👍' or 'It seems not safe to merge 🙈🌧️'."},
        {"role": "user", "content": f"Make a merge decision based on this review:\n\n{review}"}
    ])

    return f"{review}\n\n**Conclusion : {merge_decision}**"

def respond_to_comment(comment_content, file_content, conversation_history):
    messages = [
        {"role": "system", "content": "You are a helpful and informative AI assistant. Use many emojis and respond in Korean with a casual, friendly tone. Express gratitude and appreciation for questions, actively respond to user comments, and offer to review any additional questions through comments before ending the conversation."},
    ]

    # Add conversation history
    messages.extend(conversation_history)

    # Add new user message and code snippet
    messages.append({
        "role": "user",
        "content": f"I have a question about this code:\n\n```java\n{file_content}\n```\n\n{comment_content}"
    })

    return call_ai_api(messages)

def update_all_file_hashes_in_comment(pr, file_hashes):
    # Write all file hashes into one comment
    hashes_content = "\n".join([f"{file_path}: {file_hash}" for file_path, file_hash in file_hashes.items()])
    pr.create_issue_comment(f"File Hashes:\n{hashes_content}")

def get_all_file_hashes_from_comments(pr):
    comments = pr.get_issue_comments()
    file_hashes = {}
    for comment in comments:
        # Process comments starting with 'File Hashes:'
        if comment.body.startswith("File Hashes:"):
            lines = comment.body.splitlines()[1:]  # Skip the first line 'File Hashes:'
            for line in lines:
                file_path, file_hash = line.split(": ")
                file_hashes[file_path] = file_hash  # Update with new hash value if present
    return file_hashes

if __name__ == '__main__':
    print("AI review Start! ✨")
    review_pr()
    print("Review done! Check out the PR! 😊👍")
