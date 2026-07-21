package com.oldbeast.gridmode.lifeboat

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.provider.DocumentsContract
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

private const val PREFS_NAME = "lifeboat"
private const val PREF_LIFEBOAT_URI = "lifeboat_uri"

private val audioExtensions = setOf("mp3", "flac", "m4a", "ogg", "opus", "wav", "aiff", "aif")
private val coverNames = listOf("cover.png", "cover.jpg", "cover.jpeg", "folder.jpg", "folder.png")

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            LifeboatApp()
        }
    }
}

data class Album(
    val name: String,
    val uri: Uri,
    val coverUri: Uri?,
    val tracks: List<Track>,
    val modifiedAt: Long,
)

data class Track(
    val name: String,
    val uri: Uri,
)

sealed interface LibraryState {
    data object MissingFolder : LibraryState
    data object Loading : LibraryState
    data class Ready(val albums: List<Album>) : LibraryState
    data class Error(val message: String) : LibraryState
}

@Composable
fun LifeboatApp() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val player = remember { ExoPlayer.Builder(context).build() }
    var treeUri by remember { mutableStateOf(loadSavedTreeUri(context)) }
    var libraryState by remember { mutableStateOf<LibraryState>(LibraryState.MissingFolder) }
    var nowPlaying by remember { mutableStateOf<String?>(null) }

    val folderPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        context.contentResolver.takePersistableUriPermission(
            uri,
            Intent.FLAG_GRANT_READ_URI_PERMISSION,
        )
        saveTreeUri(context, uri)
        treeUri = uri
    }

    DisposableEffect(player) {
        onDispose { player.release() }
    }

    LaunchedEffect(treeUri) {
        val uri = treeUri
        if (uri == null) {
            libraryState = LibraryState.MissingFolder
            return@LaunchedEffect
        }
        libraryState = LibraryState.Loading
        libraryState = withContext(Dispatchers.IO) {
            runCatching { LibraryState.Ready(scanAlbums(context.contentResolver, uri)) }
                .getOrElse { LibraryState.Error(it.message ?: "Could not scan lifeboat folder") }
        }
    }

    MaterialTheme(
        colorScheme = darkColorScheme(
            background = Color(0xFF141414),
            surface = Color(0xFF141414),
            primary = Color(0xFFE9C46A),
            onPrimary = Color(0xFF201A0A),
            onBackground = Color(0xFFF4F0E6),
            onSurface = Color(0xFFF4F0E6),
        )
    ) {
        Surface(modifier = Modifier.fillMaxSize()) {
            LifeboatScreen(
                state = libraryState,
                nowPlaying = nowPlaying,
                onPickFolder = { folderPicker.launch(null) },
                onRefresh = {
                    treeUri?.let { uri ->
                        scope.launch {
                            libraryState = LibraryState.Loading
                            libraryState = withContext(Dispatchers.IO) {
                                runCatching { LibraryState.Ready(scanAlbums(context.contentResolver, uri)) }
                                    .getOrElse { LibraryState.Error(it.message ?: "Could not scan lifeboat folder") }
                            }
                        }
                    }
                },
                onPlayAlbum = { album ->
                    playAlbum(player, album)
                    nowPlaying = album.name
                },
            )
        }
    }
}

@Composable
private fun LifeboatScreen(
    state: LibraryState,
    nowPlaying: String?,
    onPickFolder: () -> Unit,
    onRefresh: () -> Unit,
    onPlayAlbum: (Album) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(0xFF141414))
            .padding(horizontal = 12.dp, vertical = 10.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = nowPlaying ?: "Lifeboat",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Button(onClick = onRefresh, enabled = state is LibraryState.Ready || state is LibraryState.Error) {
                Text("Refresh")
            }
        }
        Spacer(Modifier.height(10.dp))

        when (state) {
            LibraryState.MissingFolder -> EmptyState("Choose the lifeboat folder.", onPickFolder)
            LibraryState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            is LibraryState.Error -> EmptyState(state.message, onPickFolder)
            is LibraryState.Ready -> {
                if (state.albums.isEmpty()) {
                    EmptyState("No albums found in lifeboat.", onPickFolder)
                } else {
                    AlbumGrid(state.albums, onPlayAlbum)
                }
            }
        }
    }
}

@Composable
private fun EmptyState(message: String, onPickFolder: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(message, style = MaterialTheme.typography.bodyLarge)
        Spacer(Modifier.height(16.dp))
        Button(onClick = onPickFolder) {
            Text("Pick Folder")
        }
    }
}

@Composable
private fun AlbumGrid(albums: List<Album>, onPlayAlbum: (Album) -> Unit) {
    LazyVerticalGrid(
        columns = GridCells.Adaptive(minSize = 132.dp),
        contentPadding = PaddingValues(bottom = 18.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        items(albums, key = { it.uri.toString() }) { album ->
            AlbumTile(album = album, onPlayAlbum = onPlayAlbum)
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun AlbumTile(album: Album, onPlayAlbum: (Album) -> Unit) {
    Column(
        modifier = Modifier.combinedClickable(
            onClick = { onPlayAlbum(album) },
            onLongClick = { },
        )
    ) {
        CoverImage(album.coverUri)
        Spacer(Modifier.height(6.dp))
        Text(
            text = album.name,
            style = MaterialTheme.typography.bodyMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun CoverImage(uri: Uri?) {
    val context = LocalContext.current
    var bitmap by remember(uri) { mutableStateOf<ImageBitmap?>(null) }

    LaunchedEffect(uri) {
        bitmap = withContext(Dispatchers.IO) {
            uri?.let { loadBitmap(context.contentResolver, it) }
        }
    }

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .aspectRatio(1f)
            .background(Color(0xFF303030)),
        contentAlignment = Alignment.Center,
    ) {
        val image = bitmap
        if (image == null) {
            Text("GRIDMODE", style = MaterialTheme.typography.labelLarge, color = Color(0xFF9A9A9A))
        } else {
            Image(
                bitmap = image,
                contentDescription = null,
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop,
            )
        }
    }
}

private fun scanAlbums(contentResolver: ContentResolver, treeUri: Uri): List<Album> {
    return queryChildren(contentResolver, treeUri, DocumentsContract.getTreeDocumentId(treeUri))
        .filter { it.isDirectory && !it.name.startsWith(".") }
        .mapNotNull { albumDoc ->
            val children = queryChildren(contentResolver, treeUri, albumDoc.documentId)
                .filter { !it.name.startsWith(".") }
            val coverUri = children.firstMatchingCover()?.uri
            val tracks = children
                .filter { !it.isDirectory && audioExtensions.contains(it.extension) }
                .sortedWith(compareBy<DocumentEntry> { it.name.lowercase(Locale.US) }.thenBy { it.name })
                .map { Track(name = it.name, uri = it.uri) }
            if (tracks.isEmpty()) {
                null
            } else {
                Album(
                    name = albumDoc.name,
                    uri = albumDoc.uri,
                    coverUri = coverUri,
                    tracks = tracks,
                    modifiedAt = albumDoc.modifiedAt,
                )
            }
        }
        .sortedWith(compareByDescending<Album> { it.modifiedAt }.thenBy { it.name.lowercase(Locale.US) })
}

private fun List<DocumentEntry>.firstMatchingCover(): DocumentEntry? {
    for (coverName in coverNames) {
        val match = firstOrNull { !it.isDirectory && it.name.lowercase(Locale.US) == coverName }
        if (match != null) return match
    }
    return firstOrNull { !it.isDirectory && it.mimeType.startsWith("image/") }
}

private data class DocumentEntry(
    val documentId: String,
    val name: String,
    val mimeType: String,
    val modifiedAt: Long,
    val uri: Uri,
) {
    val isDirectory: Boolean = mimeType == DocumentsContract.Document.MIME_TYPE_DIR
    val extension: String = name.substringAfterLast('.', missingDelimiterValue = "")
        .lowercase(Locale.US)
}

private fun queryChildren(
    contentResolver: ContentResolver,
    treeUri: Uri,
    parentDocumentId: String,
): List<DocumentEntry> {
    val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocumentId)
    val projection = arrayOf(
        DocumentsContract.Document.COLUMN_DOCUMENT_ID,
        DocumentsContract.Document.COLUMN_DISPLAY_NAME,
        DocumentsContract.Document.COLUMN_MIME_TYPE,
        DocumentsContract.Document.COLUMN_LAST_MODIFIED,
    )
    val entries = mutableListOf<DocumentEntry>()
    contentResolver.query(childrenUri, projection, null, null, null)?.use { cursor ->
        val idIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
        val nameIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
        val mimeIndex = cursor.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
        val modifiedIndex = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED)
        while (cursor.moveToNext()) {
            val documentId = cursor.getString(idIndex) ?: continue
            entries += DocumentEntry(
                documentId = documentId,
                name = cursor.getString(nameIndex) ?: documentId.substringAfterLast('/'),
                mimeType = cursor.getString(mimeIndex) ?: "",
                modifiedAt = if (modifiedIndex >= 0) cursor.getLong(modifiedIndex) else 0,
                uri = DocumentsContract.buildDocumentUriUsingTree(treeUri, documentId),
            )
        }
    }
    return entries
}

private fun loadBitmap(contentResolver: ContentResolver, uri: Uri): ImageBitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    contentResolver.openInputStream(uri)?.use { input ->
        BitmapFactory.decodeStream(input, null, bounds)
    }
    val decodeOptions = BitmapFactory.Options().apply {
        inSampleSize = coverSampleSize(bounds.outWidth, bounds.outHeight)
    }
    return contentResolver.openInputStream(uri)?.use { input ->
        BitmapFactory.decodeStream(input, null, decodeOptions)?.asImageBitmap()
    }
}

private fun coverSampleSize(width: Int, height: Int): Int {
    var sampleSize = 1
    var sampledWidth = width
    var sampledHeight = height
    while (sampledWidth / 2 >= 512 && sampledHeight / 2 >= 512) {
        sampleSize *= 2
        sampledWidth /= 2
        sampledHeight /= 2
    }
    return sampleSize
}

private fun playAlbum(player: ExoPlayer, album: Album) {
    player.setMediaItems(album.tracks.map { MediaItem.fromUri(it.uri) })
    player.prepare()
    player.play()
}

private fun loadSavedTreeUri(context: Context): Uri? {
    val value = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .getString(PREF_LIFEBOAT_URI, null)
    return value?.let(Uri::parse)
}

private fun saveTreeUri(context: Context, uri: Uri) {
    context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        .edit()
        .putString(PREF_LIFEBOAT_URI, uri.toString())
        .apply()
}
