from django import forms

class DragAndDropFileWidget(forms.ClearableFileInput):
    template_name = 'widgets/file_dropzone.html'

    class Media:
        # KORREKTUR: Gültige Version '3.13.3' statt '3.x.x' nutzen
        js = ('https://cdn.jsdelivr.net/npm/alpinejs@3.13.3/dist/cdn.min.js',)
        css = {
            'all': ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css',)
        }