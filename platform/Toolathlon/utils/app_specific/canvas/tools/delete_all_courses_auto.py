#!/usr/bin/env python3
"""
Auto script to delete all courses belonging to this Canvas account
WARNING: This will automatically delete ALL courses without confirmation!
"""

import sys
from pathlib import Path

# Import Canvas API from parent utils module
from ..api_client import CanvasAPI

def delete_all_courses(canvas_url="http://localhost:11001", canvas_token="mcpcanvasadmintoken1"):
    """Delete all courses in the Canvas account"""
    print("🗑️  AUTO-DELETING ALL COURSES IN CANVAS ACCOUNT")
    print("=" * 60)
    
    # Initialize Canvas API
    canvas_url = canvas_url
    canvas_token = canvas_token
    
    print(f"📋 Configuration:")
    print(f"   Canvas URL: {canvas_url}")
    print(f"   Canvas Token: {canvas_token[:10]}...{canvas_token[-4:]}")
    
    canvas = CanvasAPI(canvas_url, canvas_token)
    
    # Get all courses
    print("\n🔍 Fetching all courses...")
    courses = canvas.list_courses()
    
    if not courses:
        print("✅ No courses found to delete.")
        return
    
    print(f"📚 Found {len(courses)} courses:")
    for course in courses:
        print(f"   - {course['name']} (ID: {course['id']}) - {course.get('workflow_state', 'unknown')}")
    
    # Delete each course
    print(f"\n🗑️  Auto-deleting {len(courses)} courses...")
    deleted_count = 0
    failed_count = 0
    
    for i, course in enumerate(courses, 1):
        course_id = course['id']
        course_name = course['name']
        
        print(f"[{i}/{len(courses)}] Deleting: {course_name} (ID: {course_id})...", end="")
        
        if canvas.delete_course(course_id):
            print(" ✅")
            deleted_count += 1
        else:
            print(" ❌")
            failed_count += 1
    
    # Summary
    print(f"\n📊 Deletion Summary:")
    print(f"   ✅ Successfully deleted: {deleted_count}")
    print(f"   ❌ Failed to delete: {failed_count}")
    print(f"   📚 Total courses: {len(courses)}")
    
    if deleted_count > 0:
        print(f"\n🎉 Successfully deleted {deleted_count} courses!")
    
    if failed_count > 0:
        print(f"⚠️  {failed_count} courses could not be deleted (may require special permissions)")
    
    print(f"\n✅ Course deletion process completed!")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Delete all courses in Canvas account')
    parser.add_argument('--url', default='http://localhost:11001',
                       help='Canvas base URL (default: http://localhost:11001)')
    parser.add_argument('--token', default='mcpcanvasadmintoken1',
                       help='Canvas access token (default: mcpcanvasadmintoken1)')
    
    args = parser.parse_args()
    delete_all_courses(args.url, args.token)